"""Fluence Network provider implementation."""

from __future__ import annotations

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

# Default Fluence relay multiaddr (Kras-00)
_DEFAULT_RELAY = "/dns4/kras-00.fluence.dev/tcp/19001/wss/p2p/12D3KooWSD5PToNiLQwKDXsu8JSysCwUt8BVUJEqCHcDe7P5h45e"

# Aqua function TTL in milliseconds
_DEFAULT_TTL_MS = 30_000


class FluenceProvider(IAxonProvider):
    """
    Fluence serverless P2P cloud provider.

    Deploys workloads as Fluence Spells on the Aquamarine P2P runtime.
    Sends messages by calling the spell's `handleMessage` function via Aqua.
    Results are returned synchronously (Fluence is request/response).

    Requires:
        FLUENCE_PRIVATE_KEY — 32-byte hex Ed25519 private key
                              (NOT the same as AXON_SECRET_KEY — different curve)

    Optional:
        FLUENCE_RELAY_ADDR  — relay multiaddr (default: kras-00.fluence.dev)
        FLUENCE_NETWORK     — testnet | mainnet (default: testnet)
    """

    def __init__(self) -> None:
        self._private_key: str | None = None
        self._relay: str = _DEFAULT_RELAY
        self._network: str = "testnet"
        self._connected: bool = False
        # Maps deal_id -> list of worker peer IDs
        self._workers: dict[str, list[str]] = {}
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "fluence"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Validate Fluence Ed25519 private key and CLI availability.

        Note: FLUENCE_PRIVATE_KEY is an Ed25519 key — it is distinct from
        AXON_SECRET_KEY (P-256). Do not reuse the same key.
        """
        key = os.environ.get("FLUENCE_PRIVATE_KEY") or secret_key
        if not key:
            raise AuthError(
                "Fluence Ed25519 private key required. "
                "Set FLUENCE_PRIVATE_KEY or run `axon auth fluence`."
            )
        clean = key.replace("0x", "")
        if len(clean) not in (64, 128):  # 32 or 64 bytes
            raise AuthError(
                "FLUENCE_PRIVATE_KEY must be a 32-byte (64 hex chars) or 64-byte (128 hex chars) Ed25519 key."
            )

        self._private_key = clean
        self._relay = os.environ.get("FLUENCE_RELAY_ADDR", _DEFAULT_RELAY)
        self._network = os.environ.get("FLUENCE_NETWORK", "testnet")

        _require_cli("fluence", "https://fluence.dev/docs/build/setting-up/installation")

        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy a workload as a Fluence Spell.

        Steps:
          1. Bundle entry point + inject env vars + prepend Fluence bootstrap
          2. Write spell JS to a temp directory
          3. Shell out to `fluence deploy --spell <file> --workers <N>`
          4. Parse deal ID and worker peer IDs from CLI output
        """
        if not self._connected:
            raise ProviderError("fluence", "Not connected. Call connect() first.")

        bundle_path = self._bundle(config)
        try:
            with tempfile.TemporaryDirectory(prefix="axon-fluence-") as spell_dir:
                spell_file = Path(spell_dir) / "spell.js"
                spell_file.write_bytes(bundle_path.read_bytes())

                cmd = [
                    "fluence", "deploy",
                    "--spell", str(spell_file),
                    "--no-input",
                    "--workers", str(config.replicas),
                ]
                if self._network:
                    cmd += ["--env", self._network]

                output = _run_cli(
                    cmd,
                    env={**os.environ, "FLUENCE_PRIVATE_KEY": self._private_key or ""},
                )
                deal_id, worker_ids = _parse_fluence_output(output)
                self._workers[deal_id] = worker_ids

                return Deployment(
                    id=deal_id,
                    name=config.name,
                    provider="fluence",
                    status="active" if worker_ids else "pending",
                    created_at=datetime.now(timezone.utc),
                    endpoint=None,  # Fluence uses P2P, no HTTP endpoint
                    metadata={
                        "deal_id": deal_id,
                        "worker_ids": worker_ids,
                        "relay": self._relay,
                        "network": self._network,
                    },
                )

        except subprocess.CalledProcessError as exc:
            raise DeploymentError(
                "fluence", f"fluence CLI failed:\n{exc.stderr}"
            ) from exc
        finally:
            bundle_path.unlink(missing_ok=True)

    def _bundle(self, config: DeploymentConfig) -> Path:
        """Bundle entry point with Fluence dispatch bootstrap and safe env injection."""
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("fluence", f"Entry point not found: {entry}")

        safe_env = _filter_env(config.env)
        preamble = "".join(
            f'process.env["{k}"] = {json.dumps(v)};\n' for k, v in safe_env.items()
        )
        bootstrap = _fluence_bootstrap()

        tmp = tempfile.NamedTemporaryFile(
            suffix=".js", prefix="axon-fluence-", delete=False
        )
        source = entry.read_text(encoding="utf-8")
        tmp.write((bootstrap + preamble + source).encode())
        tmp.close()
        return Path(tmp.name)

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        Call the `handleMessage` Aqua function on a deployed Fluence spell.

        processor_id is the deal_id returned by deploy().
        The call is synchronous — the result is dispatched to on_message handlers.

        Uses the `fluence run` CLI command to invoke the Aqua function.
        """
        worker_ids = self._workers.get(processor_id)
        if not worker_ids:
            raise ProviderError(
                "fluence",
                f"No workers found for deal {processor_id}. Did you deploy first?",
            )

        payload_str = json.dumps(payload) if not isinstance(payload, str) else payload

        # Build minimal Aqua script that calls handleMessage on the spell worker
        aqua_script = _build_aqua_call(worker_ids[0], self._relay, payload_str)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".aqua", prefix="axon-fluence-", delete=False
        ) as aqua_file:
            aqua_file.write(aqua_script)
            aqua_path = aqua_file.name

        try:
            output = _run_cli(
                [
                    "fluence", "run",
                    "--input", aqua_path,
                    "--function", "handleMessage",
                    "--ttl", str(_DEFAULT_TTL_MS),
                ],
                env={**os.environ, "FLUENCE_PRIVATE_KEY": self._private_key or ""},
            )

            if len(output.encode()) > _MAX_RESPONSE_BYTES:
                raise ProviderError("fluence", "Response exceeded 1 MiB limit")

            try:
                result = json.loads(output.strip())
            except json.JSONDecodeError:
                result = output.strip()

        except subprocess.CalledProcessError as exc:
            raise ProviderError("fluence", f"fluence run failed:\n{exc.stderr}") from exc
        finally:
            Path(aqua_path).unlink(missing_ok=True)

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
            raise ProviderError("fluence", "Not connected.")
        try:
            output = _run_cli(
                ["fluence", "deal", "list", "--json"],
                env={**os.environ, "FLUENCE_PRIVATE_KEY": self._private_key or ""},
            )
            items = json.loads(output)
            return [
                Deployment(
                    id=item.get("dealId", ""),
                    name=item.get("dealId", ""),
                    provider="fluence",
                    status="active" if item.get("status") == "active" else "stopped",
                    created_at=datetime.now(timezone.utc),
                    endpoint=None,
                    metadata={"worker_ids": item.get("workerIds", [])},
                )
                for item in items
            ]
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    async def health(self) -> ProviderHealth:
        """Check Fluence relay reachability via HTTP."""
        import time
        # Extract hostname from multiaddr for an HTTP probe
        relay_host_match = re.search(r"/dns4/([^/]+)/", self._relay)
        if not relay_host_match:
            return ProviderHealth(
                provider="fluence",
                status=HealthStatus.HEALTHY if self._connected else HealthStatus.UNHEALTHY,
            )
        relay_host = relay_host_match.group(1)
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"https://{relay_host}/")
            latency_ms = (time.monotonic() - start) * 1000
            # Fluence relays typically return 404 on GET / — that's fine, means reachable
            status = HealthStatus.HEALTHY if resp.status_code < 500 else HealthStatus.DEGRADED
            return ProviderHealth(provider="fluence", status=status, latency_ms=latency_ms)
        except Exception as exc:
            if self._connected:
                return ProviderHealth(provider="fluence", status=HealthStatus.DEGRADED, error=str(exc))
            return ProviderHealth(provider="fluence", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        duration_days = config.timeout_ms / 86_400_000
        flt_total = config.replicas * duration_days * 0.1  # 0.1 FLT/replica/day
        return CostEstimate(
            provider="fluence",
            token="FLT",
            amount=flt_total,
            usd_estimate=flt_total * 0.05,  # 1 FLT ~$0.05
            per_hour=False,
            breakdown={
                "replicas": float(config.replicas),
                "duration_days": duration_days,
                "flt_per_replica_per_day": 0.1,
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
            "fluence",
            f"`{name}` CLI not found in PATH. Install it: {docs_url}",
        )


def _run_cli(cmd: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
    return result.stdout


def _parse_fluence_output(output: str) -> tuple[str, list[str]]:
    """
    Parse deal ID and worker peer IDs from `fluence deploy` output.
    Returns (deal_id, worker_ids).
    """
    deal_id = ""
    worker_ids: list[str] = []

    try:
        data = json.loads(output)
        deal_id = data.get("dealId", "")
        worker_ids = data.get("workerIds", [])
        return deal_id, worker_ids
    except json.JSONDecodeError:
        pass

    # Worker peer IDs start with 12D3KooW (libp2p format)
    worker_ids = re.findall(r"12D3KooW\S+", output)
    deal_match = re.search(r"deal[Ii]d[\":\s]+([a-zA-Z0-9]+)", output)
    if deal_match:
        deal_id = deal_match.group(1)

    return deal_id, worker_ids


def _build_aqua_call(worker_id: str, relay: str, payload: str) -> str:
    """Generate a minimal Aqua script to invoke handleMessage on a spell worker."""
    escaped = payload.replace('"', '\\"')
    return f"""\
import "@fluencelabs/aqua-lib/builtin.aqua"

func handleMessage(payload: string) -> string:
    on "{worker_id}" via "{relay}":
        result <- handleMessage(payload)
    <- result

func main() -> string:
    result <- handleMessage("{escaped}")
    <- result
"""


def _fluence_bootstrap() -> str:
    """
    Fluence runtime bootstrap prepended to all spell bundles.
    Provides __axonDispatch hook and stores result in globalThis.__axonResult.
    """
    return """\
// Axon Fluence runtime bootstrap
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
