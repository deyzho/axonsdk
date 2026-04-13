"""Akash Network provider implementation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from axon.exceptions import AuthError, DeploymentError, ProviderError
from axon.providers.base import IAxonProvider
from axon.security import assert_safe_url
from axon.types import (
    CostEstimate,
    Deployment,
    DeploymentConfig,
    HealthStatus,
    Message,
    ProviderHealth,
)

_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MiB

# Blocks per hour on Akash (≈ 6s block time)
_BLOCKS_PER_HOUR = 600
# Default max uAKT per block
_DEFAULT_UAKT_PER_BLOCK = 10_000


class AkashProvider(IAxonProvider):
    """
    Akash Network containerized cloud provider.

    Deploys workloads as Docker containers on the Akash open marketplace.
    Uses the `provider-services` CLI for on-chain interactions and IPFS
    for bundle distribution.

    Requires:
        AKASH_MNEMONIC    — BIP-39 mnemonic (12 or 24 words)
        AKASH_IPFS_URL    — IPFS API endpoint (HTTPS only)
        AKASH_NODE        — Cosmos RPC node (default: https://rpc.akashnet.net:443)
        AKASH_CHAIN_ID    — Chain ID (default: akashnet-2)
        AKASH_KEY_NAME    — Key name for keyring (default: axon)
    """

    def __init__(self) -> None:
        self._mnemonic: str | None = None
        self._node: str = "https://rpc.akashnet.net:443"
        self._chain_id: str = "akashnet-2"
        self._key_name: str = "axon"
        self._connected: bool = False
        # Maps dseq → lease endpoint
        self._endpoints: dict[str, str] = {}
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "akash"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Validate Akash mnemonic and check CLI availability.
        Actual on-chain signing happens at deploy time.
        """
        mnemonic = os.environ.get("AKASH_MNEMONIC") or secret_key
        if not mnemonic:
            raise AuthError(
                "Akash mnemonic required. Set AKASH_MNEMONIC or run `axon auth akash`."
            )
        words = mnemonic.strip().split()
        if len(words) not in (12, 24):
            raise AuthError(
                f"Akash mnemonic must be 12 or 24 words, got {len(words)}."
            )
        self._mnemonic = mnemonic
        self._node = os.environ.get("AKASH_NODE", self._node)
        self._chain_id = os.environ.get("AKASH_CHAIN_ID", self._chain_id)
        self._key_name = os.environ.get("AKASH_KEY_NAME", self._key_name)

        # Check provider-services CLI is available
        _require_cli("provider-services", "https://docs.akash.network/deployments/akash-cli")

        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy a workload to Akash Network.

        Steps:
          1. Bundle entry point + inject env vars
          2. Upload bundle to IPFS -> get CID
          3. Generate SDL (Stack Definition Language) YAML
          4. Submit deployment via `provider-services` CLI
          5. Parse DSEQ and lease endpoint from CLI output
        """
        if not self._connected:
            raise ProviderError("akash", "Not connected. Call connect() first.")

        ipfs_url = os.environ.get("AKASH_IPFS_URL", "")
        if not ipfs_url:
            raise DeploymentError(
                "akash", "AKASH_IPFS_URL required. Set it in your .env or run `axon auth akash`."
            )
        _validate_ipfs_url(ipfs_url, "akash")

        bundle_path = self._bundle(config)
        try:
            bundle_cid = await self._upload_ipfs(bundle_path, ipfs_url)
            sdl_content = self._generate_sdl(config, bundle_cid)

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", prefix="axon-akash-", delete=False
            ) as sdl_file:
                sdl_file.write(sdl_content)
                sdl_path = sdl_file.name

            try:
                output = _run_cli(
                    [
                        "provider-services", "tx", "deployment", "create", sdl_path,
                        "--fees", "5000uakt",
                        "--yes",
                    ],
                    env=self._cli_env(),
                )
                dseq, endpoint = _parse_akash_output(output)
                self._endpoints[dseq] = endpoint

                return Deployment(
                    id=dseq,
                    name=config.name,
                    provider="akash",
                    status="active" if endpoint else "pending",
                    created_at=datetime.now(timezone.utc),
                    endpoint=endpoint or None,
                    metadata={
                        "dseq": dseq,
                        "bundle_cid": bundle_cid,
                        "sdl": sdl_content,
                    },
                )
            finally:
                Path(sdl_path).unlink(missing_ok=True)

        except subprocess.CalledProcessError as exc:
            raise DeploymentError(
                "akash", f"provider-services CLI failed:\n{exc.stderr}"
            ) from exc
        finally:
            bundle_path.unlink(missing_ok=True)

    def _bundle(self, config: DeploymentConfig) -> Path:
        """Bundle entry point with safe env var injection and HTTP server bootstrap."""
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("akash", f"Entry point not found: {entry}")

        safe_env = _filter_env(config.env)
        preamble = "".join(
            f'process.env["{k}"] = {json.dumps(v)};\n' for k, v in safe_env.items()
        )
        bootstrap = _akash_bootstrap()

        tmp = tempfile.NamedTemporaryFile(
            suffix=".js", prefix="axon-akash-", delete=False
        )
        source = entry.read_text(encoding="utf-8")
        tmp.write((bootstrap + preamble + source).encode())
        tmp.close()
        return Path(tmp.name)

    async def _upload_ipfs(self, bundle_path: Path, ipfs_url: str) -> str:
        """Upload bundle to IPFS via multipart/form-data POST."""
        api_key = os.environ.get("AKASH_IPFS_API_KEY")
        headers = {"Authorization": f"Basic {api_key}"} if api_key else {}

        async with httpx.AsyncClient(timeout=120.0) as client:
            with bundle_path.open("rb") as fh:
                resp = await client.post(
                    f"{ipfs_url.rstrip('/')}/api/v0/add?pin=true",
                    files={"file": ("bundle.js", fh, "application/octet-stream")},
                    headers=headers,
                )
            resp.raise_for_status()
            data = resp.json()
            cid: str = data.get("Hash", "")
            if not cid:
                raise DeploymentError("akash", f"IPFS upload returned no CID: {data}")
            return cid

    def _generate_sdl(self, config: DeploymentConfig, bundle_cid: str) -> str:
        """Generate Akash SDL (Stack Definition Language) YAML for this deployment."""
        return textwrap.dedent(f"""\
            ---
            version: "2.0"

            services:
              {config.name}:
                image: node:20-alpine
                command:
                  - sh
                  - -c
                  - |
                    wget -q -O /app/bundle.js https://ipfs.io/ipfs/{bundle_cid} \\
                    && node /app/bundle.js
                expose:
                  - port: 3000
                    as: 80
                    to:
                      - global: true

            profiles:
              compute:
                {config.name}:
                  resources:
                    cpu:
                      units: 0.5
                    memory:
                      size: {config.memory_mb}Mi
                    storage:
                      size: 1Gi
              placement:
                akash:
                  pricing:
                    {config.name}:
                      denom: uakt
                      amount: {_DEFAULT_UAKT_PER_BLOCK}

            deployment:
              {config.name}:
                akash:
                  profile: {config.name}
                  count: {config.replicas}
        """)

    def _cli_env(self) -> dict[str, str]:
        """Build environment for provider-services CLI invocations."""
        return {
            **os.environ,
            "AKASH_MNEMONIC": self._mnemonic or "",
            "AKASH_NODE": self._node,
            "AKASH_CHAIN_ID": self._chain_id,
            "AKASH_KEYRING_BACKEND": "test",
            "AKASH_FROM": self._key_name,
            "AKASH_YES": "1",
        }

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        Send a payload to the running Akash container via HTTP POST.

        processor_id is the DSEQ returned by deploy().
        The container must be running the Axon HTTP server bootstrap
        which listens on POST /message.
        """
        endpoint = self._endpoints.get(processor_id)
        if not endpoint:
            raise ProviderError(
                "akash",
                f"No endpoint for deployment {processor_id}. Did you deploy first?",
            )

        _validate_endpoint_url(endpoint, "akash")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{endpoint.rstrip('/')}/message",
                json={"payload": payload},
            )
            resp.raise_for_status()

            if len(resp.content) > _MAX_RESPONSE_BYTES:
                raise ProviderError("akash", "Response exceeded 1 MiB limit")

            try:
                result = resp.json()
            except Exception:
                result = resp.text

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
            raise ProviderError("akash", "Not connected.")
        try:
            output = _run_cli(
                ["provider-services", "query", "deployment", "list", "--owner", "self"],
                env=self._cli_env(),
            )
            data = json.loads(output)
            deployments = []
            for dep in data.get("deployments", []):
                dseq = str(dep.get("deployment", {}).get("deployment_id", {}).get("dseq", ""))
                state = dep.get("deployment", {}).get("state", "")
                deployments.append(
                    Deployment(
                        id=dseq,
                        name=dseq,
                        provider="akash",
                        status="active" if state == "active" else "stopped",
                        created_at=datetime.now(timezone.utc),
                        endpoint=self._endpoints.get(dseq),
                    )
                )
            return deployments
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return []

    async def health(self) -> ProviderHealth:
        """Check Akash RPC node reachability."""
        import time
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._node}/health")
            latency_ms = (time.monotonic() - start) * 1000
            status = HealthStatus.HEALTHY if resp.is_success else HealthStatus.DEGRADED
            return ProviderHealth(provider="akash", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="akash", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        duration_hours = config.timeout_ms / 3_600_000
        uakt_total = _DEFAULT_UAKT_PER_BLOCK * _BLOCKS_PER_HOUR * duration_hours * config.replicas
        akt_total = uakt_total / 1_000_000
        # AKT ~$0.30 (update with live oracle)
        usd = akt_total * 0.30
        return CostEstimate(
            provider="akash",
            token="AKT",
            amount=akt_total,
            usd_estimate=usd,
            per_hour=False,
            breakdown={
                "uakt_per_block": float(_DEFAULT_UAKT_PER_BLOCK),
                "blocks_per_hour": float(_BLOCKS_PER_HOUR),
                "duration_hours": duration_hours,
                "replicas": float(config.replicas),
            },
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_MNEMONIC", "_PRIVATE_KEY")


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not any(k.upper().endswith(s) for s in _SECRET_SUFFIXES)}


def _validate_ipfs_url(url: str, provider: str) -> None:
    assert_safe_url(url, provider, "IPFS URL")


def _validate_endpoint_url(url: str, provider: str) -> None:
    assert_safe_url(url, provider, "Endpoint")


def _require_cli(name: str, docs_url: str) -> None:
    import shutil
    if not shutil.which(name):
        raise ProviderError(
            "akash",
            f"`{name}` CLI not found in PATH. Install it: {docs_url}",
        )


def _run_cli(cmd: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout


def _parse_akash_output(output: str) -> tuple[str, str]:
    """
    Parse DSEQ and lease endpoint from provider-services CLI output.
    Returns (dseq, endpoint).
    """
    dseq = ""
    endpoint = ""
    try:
        data = json.loads(output)
        dseq = str(
            data.get("deployment", {})
            .get("deployment_id", {})
            .get("dseq", "")
        )
        services = data.get("forwarded_ports", {})
        for svc in services.values():
            for port in svc:
                if port.get("externalPort") == 80:
                    host = port.get("host", "")
                    ext_port = port.get("externalPort", 80)
                    endpoint = f"https://{host}:{ext_port}"
                    break
    except (json.JSONDecodeError, AttributeError):
        dseq_match = re.search(r"dseq[\":\s]+(\d+)", output)
        if dseq_match:
            dseq = dseq_match.group(1)
        url_match = re.search(r"https://[^\s]+", output)
        if url_match:
            endpoint = url_match.group(0).rstrip(",")

    return dseq, endpoint


def _akash_bootstrap() -> str:
    """
    Minimal Node.js HTTP server bootstrap prepended to all Akash bundles.
    Listens on process.env.PORT (default 3000) and exposes:
      GET  /health  -> 200 "ok"
      POST /message -> calls handler, returns JSON result
    """
    return textwrap.dedent("""\
        // Axon Akash runtime bootstrap
        const http = require('http');
        const PORT = process.env.PORT || 3000;
        let _pendingResolve = null;

        globalThis.axon = {
          fulfill: (result) => { if (_pendingResolve) _pendingResolve(result); },
          http: { GET: (url, cb) => fetch(url).then(r => r.json()).then(cb).catch(cb) },
        };

        http.createServer((req, res) => {
          if (req.method === 'GET' && req.url === '/health') {
            res.writeHead(200); res.end('ok'); return;
          }
          if (req.method === 'POST' && req.url === '/message') {
            let body = '';
            req.on('data', c => body += c);
            req.on('end', () => {
              const { payload } = JSON.parse(body);
              new Promise(resolve => { _pendingResolve = resolve; handleMessage(payload); })
                .then(result => {
                  res.writeHead(200, {'Content-Type':'application/json'});
                  res.end(JSON.stringify(result));
                })
                .catch(e => { res.writeHead(500); res.end(JSON.stringify({error: e.message})); });
            });
            return;
          }
          res.writeHead(404); res.end('not found');
        }).listen(PORT, () => console.log('[axon:akash] listening on', PORT));

    """)
