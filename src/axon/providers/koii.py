"""Koii Network provider implementation."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from axon.exceptions import AuthError, DeploymentError, ProviderError
from axon.providers.base import IAxonProvider
from axon.types import (
    CostEstimate,
    Deployment,
    DeploymentConfig,
    HealthStatus,
    Message,
    ProviderHealth,
)

_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MiB

# Polling config for send()
_POLL_INTERVAL_S = 2.0
_POLL_TIMEOUT_S = 30.0

# Koii task name: alphanumeric + hyphens, max 64 chars
_TASK_NAME_RE = re.compile(r"[^a-zA-Z0-9\-]")

# Base58 alphabet (Bitcoin/Solana)
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class KoiiProvider(IAxonProvider):
    """
    Koii community task node provider.

    Deploys workloads as Koii Tasks — on-chain registered bundles served
    from IPFS and executed by community-run task nodes.

    Requires:
        KOII_PRIVATE_KEY  — base58 or hex Solana-compatible keypair
                            (32-byte seed or 64-byte full keypair)
        KOII_IPFS_URL     — IPFS API endpoint (HTTPS only)

    Optional:
        KOII_NETWORK      — mainnet | testnet (default: mainnet)
        KOII_RPC_URL      — RPC node (default: https://mainnet.koii.network)
        KOII_TASK_ID      — reuse an existing task ID instead of deploying a new one
    """

    DEFAULT_RPC = "https://mainnet.koii.network"

    def __init__(self) -> None:
        self._private_key: str | None = None           # raw base58 or hex
        self._rpc_url: str = self.DEFAULT_RPC
        self._network: str = "mainnet"
        self._connected: bool = False
        # Maps task_id -> node endpoint URL
        self._node_endpoints: dict[str, str] = {}
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "koii"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Validate the Koii private key and check CLI/RPC availability.

        Accepts:
          - base58-encoded 32-byte seed (Solana standard)
          - base58-encoded 64-byte full keypair
          - hex-encoded 32 or 64 bytes
        """
        key = os.environ.get("KOII_PRIVATE_KEY") or secret_key
        if not key:
            raise AuthError(
                "Koii private key required. Set KOII_PRIVATE_KEY or run `axon auth koii`."
            )

        # Normalise to bytes then back to base58 for the CLI
        key_bytes = _decode_koii_key(key)
        if len(key_bytes) not in (32, 64):
            raise AuthError(
                f"Koii private key must be 32 or 64 bytes, got {len(key_bytes)}."
            )

        self._private_key = key
        self._rpc_url = os.environ.get("KOII_RPC_URL", self.DEFAULT_RPC)
        self._network = os.environ.get("KOII_NETWORK", "mainnet")

        # create-task-cli is needed for deploy; check now so the error is clear
        # (send only needs httpx, so we don't fail hard here — just warn)
        import shutil
        if not shutil.which("create-task-cli"):
            # Not fatal at connect — only fatal if deploy() is called
            pass

        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy a workload as a Koii Task.

        Steps:
          1. Bundle entry point + inject env vars + prepend Koii bootstrap
          2. Upload bundle to IPFS -> CID
          3. Sanitise task name
          4. Shell out to `create-task-cli` to register task on-chain
          5. Parse task ID and node endpoints from CLI output
        """
        if not self._connected:
            raise ProviderError("koii", "Not connected. Call connect() first.")

        _require_cli(
            "create-task-cli",
            "https://docs.koii.network/develop/command-line-tool/task-node",
        )

        ipfs_url = os.environ.get("KOII_IPFS_URL", "")
        if not ipfs_url:
            raise DeploymentError(
                "koii", "KOII_IPFS_URL required. Set it in your .env or run `axon auth koii`."
            )
        if not ipfs_url.startswith("https://"):
            raise DeploymentError("koii", "KOII_IPFS_URL must use HTTPS.")

        bundle_path = self._bundle(config)
        try:
            bundle_cid = await self._upload_ipfs(bundle_path, ipfs_url)

            task_name = _sanitise_task_name(config.name)

            cmd = [
                "create-task-cli",
                "--task-name", task_name,
                "--cid", bundle_cid,
                "--replicas", str(config.replicas),
                "--minimum-stake-amount", "1",
                "--no-prompt",
            ]
            if self._network == "testnet":
                cmd += ["--cluster", "testnet"]

            output = _run_cli(
                cmd,
                env={**os.environ, "KOII_PRIVATE_KEY": self._private_key or ""},
            )
            task_id, node_endpoints = _parse_koii_output(output)

            for ep in node_endpoints:
                self._node_endpoints[task_id] = ep  # store first/last

            return Deployment(
                id=task_id,
                name=config.name,
                provider="koii",
                status="active" if node_endpoints else "pending",
                created_at=datetime.now(timezone.utc),
                endpoint=node_endpoints[0] if node_endpoints else None,
                metadata={
                    "task_id": task_id,
                    "bundle_cid": bundle_cid,
                    "node_endpoints": node_endpoints,
                    "network": self._network,
                },
            )

        except subprocess.CalledProcessError as exc:
            raise DeploymentError("koii", f"create-task-cli failed:\n{exc.stderr}") from exc
        finally:
            bundle_path.unlink(missing_ok=True)

    def _bundle(self, config: DeploymentConfig) -> Path:
        """Bundle entry point with Koii dispatch bootstrap and safe env injection."""
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("koii", f"Entry point not found: {entry}")

        safe_env = _filter_env(config.env)
        preamble = "".join(
            f'process.env["{k}"] = {json.dumps(v)};\n' for k, v in safe_env.items()
        )
        bootstrap = _koii_bootstrap()

        tmp = tempfile.NamedTemporaryFile(
            suffix=".js", prefix="axon-koii-", delete=False
        )
        source = entry.read_text(encoding="utf-8")
        tmp.write((bootstrap + preamble + source).encode())
        tmp.close()
        return Path(tmp.name)

    async def _upload_ipfs(self, bundle_path: Path, ipfs_url: str) -> str:
        """Upload bundle to IPFS as raw octet-stream POST."""
        api_key = os.environ.get("KOII_IPFS_API_KEY")
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Basic {api_key}"

        async with httpx.AsyncClient(timeout=120.0) as client:
            with bundle_path.open("rb") as fh:
                resp = await client.post(
                    f"{ipfs_url.rstrip('/')}/api/v0/add",
                    content=fh.read(),
                    headers={**headers, "Content-Type": "application/octet-stream"},
                )
            resp.raise_for_status()
            data = resp.json()
            # Support both Infura-style { Hash } and go-ipfs DAG-JSON { cid: { '/': '...' } }
            cid: str = data.get("Hash") or (data.get("cid") or {}).get("/", "")
            if not cid:
                raise DeploymentError("koii", f"IPFS upload returned no CID: {data}")
            return cid

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        Send a payload to a running Koii task node and poll for the result.

        processor_id is the task_id returned by deploy().

        Communication protocol:
          1. POST payload to {node}/task/{task_id}/input
          2. Poll GET  {node}/task/{task_id}/result until non-empty (max 30s)
          3. Dispatch result to on_message handlers
        """
        node_endpoint = self._node_endpoints.get(processor_id)
        if not node_endpoint:
            # Try the env-pinned task ID path
            rpc_task_id = os.environ.get("KOII_TASK_ID")
            if rpc_task_id == processor_id:
                node_endpoint = self._rpc_url
            else:
                raise ProviderError(
                    "koii",
                    f"No node endpoint for task {processor_id}. Did you deploy first?",
                )

        if not node_endpoint.startswith("https://"):
            raise ProviderError("koii", "Node endpoint must use HTTPS.")

        task_base = f"{node_endpoint.rstrip('/')}/task/{processor_id}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: submit input
            submit_resp = await client.post(
                f"{task_base}/input",
                json={"payload": payload},
            )
            submit_resp.raise_for_status()

            # Step 2: poll for result
            deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT_S
            result: Any = None
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL_S)
                poll_resp = await client.get(f"{task_base}/result")
                if poll_resp.status_code == 200:
                    body = poll_resp.content
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ProviderError("koii", "Response exceeded 1 MiB limit")
                    if body:
                        try:
                            result = poll_resp.json()
                        except Exception:
                            result = poll_resp.text
                        break

        if result is None:
            raise ProviderError(
                "koii",
                f"Task {processor_id} did not produce a result within {_POLL_TIMEOUT_S}s.",
            )

        msg = Message(processor_id=processor_id, payload=result)
        for handler in list(self._message_handlers):
            handler(msg)

    def on_message(self, handler: Callable[[Message], None]) -> Callable[[], None]:
        self._message_handlers.append(handler)
        return lambda: self._message_handlers.remove(handler)

    # ------------------------------------------------------------------
    # Listings & health
    # ------------------------------------------------------------------

    async def list_deployments(self) -> list[Deployment]:
        if not self._connected:
            raise ProviderError("koii", "Not connected.")
        try:
            output = _run_cli(
                ["create-task-cli", "--list-tasks", "--json"],
                env={**os.environ, "KOII_PRIVATE_KEY": self._private_key or ""},
            )
            items = json.loads(output)
            return [
                Deployment(
                    id=item.get("taskId", ""),
                    name=item.get("taskName", item.get("taskId", "")),
                    provider="koii",
                    status="active" if item.get("isRunning") else "stopped",
                    created_at=datetime.now(timezone.utc),
                    endpoint=self._node_endpoints.get(item.get("taskId", "")),
                    metadata={"cid": item.get("cid", "")},
                )
                for item in items
            ]
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    async def health(self) -> ProviderHealth:
        """Check Koii RPC node reachability."""
        import time
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._rpc_url}/health")
            latency_ms = (time.monotonic() - start) * 1000
            status = HealthStatus.HEALTHY if resp.is_success else HealthStatus.DEGRADED
            return ProviderHealth(provider="koii", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="koii", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        # Koii uses staking — cost is minimum stake per replica
        stake_per_replica = 1.0  # 1 KOII minimum stake
        return CostEstimate(
            provider="koii",
            token="KOII",
            amount=stake_per_replica * config.replicas,
            usd_estimate=stake_per_replica * config.replicas * 0.02,  # 1 KOII ~$0.02
            per_hour=False,
            breakdown={
                "stake_per_replica": stake_per_replica,
                "replicas": float(config.replicas),
            },
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_MNEMONIC", "_PRIVATE_KEY")


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not any(k.upper().endswith(s) for s in _SECRET_SUFFIXES)}


def _require_cli(name: str, docs_url: str) -> None:
    import shutil
    if not shutil.which(name):
        raise ProviderError(
            "koii",
            f"`{name}` CLI not found in PATH. Install it: {docs_url}",
        )


def _run_cli(cmd: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
    return result.stdout


def _decode_koii_key(key: str) -> bytes:
    """Decode a Koii private key from base58 or hex to raw bytes."""
    clean = key.strip()
    # Try hex first
    hex_clean = clean.replace("0x", "")
    if re.fullmatch(r"[0-9a-fA-F]+", hex_clean) and len(hex_clean) in (64, 128):
        return bytes.fromhex(hex_clean)
    # Try base58
    try:
        return _b58decode(clean)
    except Exception as exc:
        raise AuthError(f"Could not decode Koii private key (expected hex or base58): {exc}") from exc


def _b58decode(s: str) -> bytes:
    """Decode a base58-encoded string to bytes."""
    n = 0
    for char in s:
        n = n * 58 + _B58_ALPHABET.index(char)
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    padding = len(s) - len(s.lstrip("1"))
    return b"\x00" * padding + result


def _sanitise_task_name(name: str) -> str:
    """Sanitise a task name: alphanumeric + hyphens, max 64 chars, no path traversal."""
    safe = _TASK_NAME_RE.sub("-", name)[:64].strip("-")
    if not safe:
        safe = "axon-task"
    return safe


def _parse_koii_output(output: str) -> tuple[str, list[str]]:
    """
    Parse task ID and node endpoints from `create-task-cli` output.
    Returns (task_id, node_endpoints).
    """
    task_id = ""
    node_endpoints: list[str] = []

    try:
        data = json.loads(output)
        task_id = data.get("taskId", "")
        node_endpoints = data.get("nodeEndpoints", [])
        return task_id, node_endpoints
    except json.JSONDecodeError:
        pass

    # base58 task ID (32-44 chars, no 0/O/I/l)
    b58_matches = re.findall(r"\b[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{32,44}\b", output)
    if b58_matches:
        task_id = b58_matches[0]

    # HTTPS node endpoints
    node_endpoints = re.findall(r"https://[^\s,\"]+", output)

    return task_id, node_endpoints


def _koii_bootstrap() -> str:
    """
    Koii runtime bootstrap prepended to all task bundles.
    Provides __axonDispatch hook and stores result in globalThis.__axonResult.
    The task node polls for __axonResult via the HTTP task result endpoint.
    """
    return """\
// Axon Koii runtime bootstrap
globalThis.__axonResult = undefined;

globalThis.axon = {
  fulfill: (result) => { globalThis.__axonResult = result; },
  http: { GET: (url, cb) => fetch(url).then(r => r.json()).then(cb).catch(cb) },
};

function __axonDispatch(payload) {
  const parsed = (() => { try { return JSON.parse(payload); } catch { return payload; } })();
  if (typeof handleMessage === 'function') {
    const result = handleMessage(parsed);
    if (result && typeof result.then === 'function') {
      result.then(r => { globalThis.__axonResult = r; });
    } else {
      globalThis.__axonResult = result;
    }
  }
  return JSON.stringify(globalThis.__axonResult);
}

"""
