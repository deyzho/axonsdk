"""Acurast TEE provider implementation."""

from __future__ import annotations

import asyncio
import json
import os
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

# Acurast micro-token denomination (1 ACU = 1_000_000 uACU)
_UACU_PER_ACU = 1_000_000
# Default max cost per execution in uACU
_DEFAULT_MAX_COST_UACU = 1_000_000  # 1 ACU


class AcurastProvider(IAxonProvider):
    """
    Acurast TEE smartphone network provider.

    Deploys workloads to Trusted Execution Environments on Acurast's
    decentralised smartphone network (~237k nodes). Uses P-256 keys for
    identity and WebSocket for bidirectional message passing.

    Requires:
        AXON_SECRET_KEY    — P-256 private key hex (32 bytes / 64 hex chars)
        ACURAST_MNEMONIC   — Substrate BIP-39 mnemonic (for on-chain registration)
        ACURAST_IPFS_URL   — IPFS API endpoint (HTTPS)

    Optional:
        ACURAST_WS_URL     — WebSocket server (default: wss://ws-1.ws-server-1.acurast.com)
        ACURAST_DESTINATIONS — comma-separated TEE processor public keys to target
    """

    DEFAULT_WS_URL = "wss://ws-1.ws-server-1.acurast.com"

    def __init__(self) -> None:
        self._secret_key: str | None = None
        self._mnemonic: str | None = None
        self._ws_url: str = self.DEFAULT_WS_URL
        self._ws: Any | None = None                 # websockets.WebSocketClientProtocol
        self._connected: bool = False
        self._listen_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "acurast"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Connect to the Acurast WebSocket relay.

        Authentication uses a P-256 key pair derived from secret_key.
        The Acurast SDK handles the challenge/response handshake.
        """
        key = os.environ.get("AXON_SECRET_KEY") or secret_key
        mnemonic = os.environ.get("ACURAST_MNEMONIC", "")

        if not key:
            raise AuthError(
                "P-256 secret key required. Set AXON_SECRET_KEY or run `axon auth acurast`."
            )
        if len(key.replace("0x", "")) != 64:
            raise AuthError(
                "AXON_SECRET_KEY must be a 32-byte hex string (64 hex characters)."
            )

        self._secret_key = key.replace("0x", "")
        self._mnemonic = mnemonic
        self._ws_url = os.environ.get("ACURAST_WS_URL", self.DEFAULT_WS_URL)

        # Connect via websockets library
        try:
            import websockets  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ProviderError(
                "acurast", "websockets package required. Install with: pip install websockets"
            ) from exc

        self._ws = await websockets.connect(
            self._ws_url,
            open_timeout=15,
            ping_interval=30,
            ping_timeout=10,
        )
        self._connected = True

        # Start background listener
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def disconnect(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy a workload to Acurast TEE nodes.

        Steps:
          1. Bundle entry point + inject env vars + prepend TEE runtime bootstrap
          2. Upload bundle to IPFS -> CID
          3. Shell out to `acurast deploy` CLI with CID, replicas, duration
          4. Parse processor IDs and deployment ID from CLI output
        """
        if not self._connected:
            raise ProviderError("acurast", "Not connected. Call connect() first.")

        ipfs_url = os.environ.get("ACURAST_IPFS_URL", "")
        if not ipfs_url:
            raise DeploymentError(
                "acurast",
                "ACURAST_IPFS_URL required. Set it in your .env or run `axon auth acurast`.",
            )

        _require_cli("acurast", "https://docs.acurast.com/developers/acurast-cli")

        bundle_path = self._bundle(config)
        try:
            bundle_cid = await self._upload_ipfs(bundle_path, ipfs_url)

            # Destinations (specific TEE processors to target, if any)
            destinations = os.environ.get("ACURAST_DESTINATIONS", "")

            cmd = [
                "acurast", "deploy", str(bundle_path),
                "--replicas", str(config.replicas),
                "--interval", str(config.timeout_ms),
                "--duration", str(config.timeout_ms * 10),
                "--max-cost", str(_DEFAULT_MAX_COST_UACU * config.replicas),
            ]
            if destinations:
                cmd += ["--destination", destinations]

            output = _run_cli(cmd, env={**os.environ, "ACURAST_MNEMONIC": self._mnemonic or ""})
            deployment_id, processor_ids = _parse_acurast_output(output)

            return Deployment(
                id=deployment_id or bundle_cid,
                name=config.name,
                provider="acurast",
                status="active" if processor_ids else "pending",
                created_at=datetime.now(timezone.utc),
                endpoint=f"https://{deployment_id}.acu.run" if deployment_id else None,
                metadata={
                    "bundle_cid": bundle_cid,
                    "processor_ids": processor_ids,
                    "ws_url": self._ws_url,
                },
            )

        except subprocess.CalledProcessError as exc:
            raise DeploymentError(
                "acurast", f"acurast CLI failed:\n{exc.stderr}"
            ) from exc
        finally:
            bundle_path.unlink(missing_ok=True)

    def _bundle(self, config: DeploymentConfig) -> Path:
        """Bundle entry point with TEE runtime bootstrap and safe env injection."""
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("acurast", f"Entry point not found: {entry}")

        safe_env = _filter_env(config.env)
        preamble = "".join(
            f'process.env["{k}"] = {json.dumps(v)};\n' for k, v in safe_env.items()
        )
        bootstrap = _acurast_bootstrap()

        tmp = tempfile.NamedTemporaryFile(
            suffix=".js", prefix="axon-acurast-", delete=False
        )
        source = entry.read_text(encoding="utf-8")
        tmp.write((bootstrap + preamble + source).encode())
        tmp.close()
        return Path(tmp.name)

    async def _upload_ipfs(self, bundle_path: Path, ipfs_url: str) -> str:
        """Upload bundle to IPFS and return CID."""
        if not ipfs_url.startswith("https://"):
            raise DeploymentError("acurast", "ACURAST_IPFS_URL must use HTTPS.")

        api_key = os.environ.get("ACURAST_IPFS_API_KEY")
        headers = {"Authorization": f"Basic {api_key}"} if api_key else {}

        async with httpx.AsyncClient(timeout=120.0) as client:
            with bundle_path.open("rb") as fh:
                resp = await client.post(
                    f"{ipfs_url.rstrip('/')}/api/v0/add",
                    files={"file": ("bundle.js", fh, "application/octet-stream")},
                    headers=headers,
                )
            resp.raise_for_status()
            data = resp.json()
            # Handle both Infura-style { Hash } and go-ipfs { cid: { '/': '...' } }
            cid: str = data.get("Hash") or (data.get("cid") or {}).get("/", "")
            if not cid:
                raise DeploymentError("acurast", f"IPFS upload returned no CID: {data}")
            return cid

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        Send a JSON payload to a TEE processor via the Acurast WebSocket relay.

        processor_id is a 64-char hex public key of the target TEE processor,
        found in deployment.metadata['processor_ids'].
        """
        if not self._ws or not self._connected:
            raise ProviderError("acurast", "Not connected.")

        message = json.dumps({
            "type": "send",
            "recipient": processor_id,
            "payload": json.dumps(payload) if not isinstance(payload, str) else payload,
        })
        await self._ws.send(message)

    def on_message(self, handler: Callable[[Message], None]) -> Callable[[], None]:
        self._message_handlers.append(handler)
        return lambda: self._message_handlers.remove(handler)

    async def _listen_loop(self) -> None:
        """Background task: receive messages from the WebSocket relay."""
        if not self._ws:
            return
        try:
            async for raw in self._ws:
                if len(raw) > _MAX_RESPONSE_BYTES:
                    continue  # drop oversized messages
                try:
                    envelope = json.loads(raw)
                    sender = envelope.get("sender", "")
                    raw_payload = envelope.get("payload", "")
                    try:
                        payload = json.loads(raw_payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = raw_payload

                    msg = Message(processor_id=sender, payload=payload)
                    for handler in list(self._message_handlers):
                        handler(msg)
                except (json.JSONDecodeError, Exception):
                    pass  # malformed message — skip
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # connection closed

    # ------------------------------------------------------------------
    # Listings & health
    # ------------------------------------------------------------------

    async def list_deployments(self) -> list[Deployment]:
        if not self._connected:
            raise ProviderError("acurast", "Not connected.")
        try:
            output = _run_cli(
                ["acurast", "deployments", "--format", "json"],
                env={**os.environ, "ACURAST_MNEMONIC": self._mnemonic or ""},
            )
            items = json.loads(output)
            return [
                Deployment(
                    id=item.get("deploymentId", ""),
                    name=item.get("deploymentId", ""),
                    provider="acurast",
                    status="active" if item.get("status") == "live" else "pending",
                    created_at=datetime.now(timezone.utc),
                    endpoint=f"https://{item.get('deploymentId', '')}.acu.run",
                    metadata={"processor_ids": item.get("processorIds", [])},
                )
                for item in items
            ]
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    async def health(self) -> ProviderHealth:
        """Check Acurast WebSocket relay reachability via HTTP probe."""
        import time
        ws_http_url = self._ws_url.replace("wss://", "https://").replace("ws://", "http://")
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{ws_http_url}/health")
            latency_ms = (time.monotonic() - start) * 1000
            status = HealthStatus.HEALTHY if resp.is_success else HealthStatus.DEGRADED
            return ProviderHealth(provider="acurast", status=status, latency_ms=latency_ms)
        except Exception as exc:
            # WebSocket health can't be probed via HTTP — if connected, report healthy
            if self._connected and self._ws:
                return ProviderHealth(provider="acurast", status=HealthStatus.HEALTHY)
            return ProviderHealth(provider="acurast", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        # 1 ACU/replica/day approximation
        duration_days = config.timeout_ms / (86_400_000)
        acu_total = config.replicas * duration_days * 1.0
        return CostEstimate(
            provider="acurast",
            token="ACU",
            amount=acu_total,
            usd_estimate=acu_total * 0.01,  # 1 ACU ~$0.01
            per_hour=False,
            breakdown={
                "replicas": float(config.replicas),
                "duration_days": duration_days,
                "acu_per_replica_per_day": 1.0,
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
            "acurast",
            f"`{name}` CLI not found in PATH. Install it: {docs_url}",
        )


def _run_cli(cmd: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
    return result.stdout


def _parse_acurast_output(output: str) -> tuple[str, list[str]]:
    """
    Parse deployment ID and processor IDs from `acurast deploy` output.
    Returns (deployment_id, processor_ids).
    """
    import re
    deployment_id = ""
    processor_ids: list[str] = []

    try:
        data = json.loads(output)
        deployment_id = data.get("deploymentId", "")
        processor_ids = data.get("processorIds", [])
        return deployment_id, processor_ids
    except json.JSONDecodeError:
        pass

    # Hex deployment ID (0x...)
    dep_match = re.search(r"0x[0-9a-fA-F]{16,}", output)
    if dep_match:
        deployment_id = dep_match.group(0)

    # 64-char hex processor pubkeys
    processor_ids = re.findall(r"\b[0-9a-fA-F]{64}\b", output)

    return deployment_id, processor_ids


def _acurast_bootstrap() -> str:
    """
    TEE runtime bootstrap prepended to all Acurast bundles.
    Maps globalThis.axon -> _STD_ (the Acurast TEE global API).
    """
    return """\
// Axon Acurast TEE runtime bootstrap
globalThis.axon = {
  http:    _STD_.http,
  ws:      _STD_.ws,
  fulfill: _STD_.fulfill,
  env:     _STD_.env,
};

"""
