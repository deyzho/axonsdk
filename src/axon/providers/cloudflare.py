"""Cloudflare Workers provider implementation."""

from __future__ import annotations

import json
import os
import re
import time
import zipfile
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

_CF_API = "https://api.cloudflare.com/client/v4"

# Cloudflare Workers pricing
# Free: 100k req/day; Paid: $0.30/million after first 10M
_PRICE_PER_MILLION_REQ = 0.30


class CloudflareProvider(IAxonProvider):
    """
    Cloudflare Workers provider.

    Deploys JavaScript/TypeScript workloads to Cloudflare's global edge network
    (300+ PoPs worldwide). Sub-millisecond cold starts, no infrastructure to manage.

    Requires:
        CF_API_TOKEN   — Cloudflare API token (with Workers:Edit permission)
        CF_ACCOUNT_ID  — Cloudflare account ID
    """

    def __init__(self) -> None:
        self._api_token: str | None = None
        self._account_id: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._connected: bool = False
        self._worker_urls: dict[str, str] = {}
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "cloudflare"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Authenticate with the Cloudflare API using CF_API_TOKEN.
        """
        token = os.environ.get("CF_API_TOKEN") or secret_key
        account_id = os.environ.get("CF_ACCOUNT_ID", "")

        if not token:
            raise AuthError(
                "Cloudflare API token required. Set CF_API_TOKEN or run `axon auth cloudflare`."
            )
        if not account_id:
            raise AuthError(
                "CF_ACCOUNT_ID required. Find it in the Cloudflare dashboard right sidebar."
            )

        self._api_token = token
        self._account_id = account_id
        self._client = httpx.AsyncClient(
            base_url=_CF_API,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30.0,
        )

        # Validate token
        try:
            resp = await self._client.get("/user/tokens/verify")
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                raise AuthError(f"Cloudflare token invalid: {data.get('errors')}")
        except httpx.HTTPStatusError as exc:
            raise AuthError(f"Cloudflare auth failed: {exc.response.status_code}") from exc

        self._connected = True

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy a Worker script to Cloudflare's global edge.

        The entry point is uploaded as a Worker module. The script must
        export a default fetch handler:

            export default { fetch(request, env) { ... } }
        """
        if not self._client or not self._connected:
            raise ProviderError("cloudflare", "Not connected. Call connect() first.")

        script_name = _sanitise_worker_name(config.name)
        entry = Path(config.entry_point)

        if not entry.exists():
            raise DeploymentError("cloudflare", f"Entry point not found: {entry}")

        source = entry.read_text(encoding="utf-8")
        # Inject env vars as Worker bindings preamble (vars, not secrets)
        safe_env = _filter_env(config.env)
        preamble = _build_worker_preamble(safe_env)
        full_source = preamble + source

        # Upload using multipart Workers API
        import io
        metadata = {
            "main_module": "worker.js",
            "bindings": [],
            "compatibility_date": "2024-01-01",
        }

        try:
            resp = await self._client.put(
                f"/accounts/{self._account_id}/workers/scripts/{script_name}",
                content=_build_worker_multipart(full_source, metadata),
                headers={
                    "Authorization": f"Bearer {self._api_token}",
                    "Content-Type": "multipart/form-data; boundary=axon-worker-boundary",
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                raise DeploymentError("cloudflare", f"Worker upload failed: {data.get('errors')}")

            # The worker is available at {script_name}.{subdomain}.workers.dev
            subdomain_resp = await self._client.get(
                f"/accounts/{self._account_id}/workers/subdomain"
            )
            subdomain = ""
            if subdomain_resp.is_success:
                subdomain = subdomain_resp.json().get("result", {}).get("subdomain", "")

            worker_url = (
                f"https://{script_name}.{subdomain}.workers.dev" if subdomain
                else f"https://{script_name}.workers.dev"
            )
            self._worker_urls[script_name] = worker_url

            return Deployment(
                id=script_name,
                name=config.name,
                provider="cloudflare",
                status="active",
                created_at=datetime.now(timezone.utc),
                endpoint=worker_url,
                metadata={
                    "script_name": script_name,
                    "account_id": self._account_id,
                    "worker_url": worker_url,
                },
            )

        except httpx.HTTPStatusError as exc:
            raise DeploymentError(
                "cloudflare",
                f"Worker deploy failed (HTTP {exc.response.status_code}): {exc.response.text}",
            ) from exc

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        Invoke a Worker via its workers.dev URL with a POST request.
        """
        endpoint = self._worker_urls.get(processor_id)
        if not endpoint:
            raise ProviderError(
                "cloudflare",
                f"No URL for Worker {processor_id}. Did you deploy first?",
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{endpoint}/message",
                json={"payload": payload},
                headers={"Authorization": f"Bearer {self._api_token}"},
            )
            resp.raise_for_status()
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
        if not self._client or not self._connected:
            raise ProviderError("cloudflare", "Not connected.")
        resp = await self._client.get(
            f"/accounts/{self._account_id}/workers/scripts"
        )
        if not resp.is_success:
            return []
        scripts = resp.json().get("result", [])
        return [
            Deployment(
                id=s.get("id", ""),
                name=s.get("id", ""),
                provider="cloudflare",
                status="active",
                created_at=datetime.fromisoformat(
                    s["created_on"].replace("Z", "+00:00")
                ) if "created_on" in s else datetime.now(timezone.utc),
                endpoint=self._worker_urls.get(s.get("id", "")),
                metadata={"account_id": self._account_id},
            )
            for s in scripts
        ]

    async def health(self) -> ProviderHealth:
        """Probe Cloudflare API."""
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{_CF_API}/ips")
            latency_ms = (time.monotonic() - start) * 1000
            status = HealthStatus.HEALTHY if resp.is_success else HealthStatus.DEGRADED
            return ProviderHealth(provider="cloudflare", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="cloudflare", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        # Workers Paid: $5/month + $0.30/million requests after first 10M
        # Approximate for a single invocation
        est = config.replicas * _PRICE_PER_MILLION_REQ / 1_000_000
        return CostEstimate(
            provider="cloudflare",
            token="USD",
            amount=est,
            usd_estimate=est,
            per_hour=False,
            breakdown={"price_per_million": _PRICE_PER_MILLION_REQ, "requests": float(config.replicas)},
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_MNEMONIC", "_PRIVATE_KEY")


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not any(k.upper().endswith(s) for s in _SECRET_SUFFIXES)}


def _sanitise_worker_name(name: str) -> str:
    return re.sub(r"[^a-z0-9\-]", "-", name.lower())[:63].strip("-") or "axon-worker"


def _build_worker_preamble(env: dict[str, str]) -> str:
    """Build a JS preamble that sets env vars as globalThis constants."""
    lines = ["// Axon Cloudflare Workers bootstrap\n"]
    for k, v in env.items():
        lines.append(f"globalThis.{k} = {json.dumps(v)};\n")
    lines.append("\n")
    return "".join(lines)


def _build_worker_multipart(source: str, metadata: dict[str, Any]) -> bytes:
    """Build a multipart/form-data body for the Workers upload API."""
    boundary = b"axon-worker-boundary"
    body_parts = [
        b"--" + boundary + b"\r\n",
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(metadata).encode(),
        b"\r\n--" + boundary + b"\r\n",
        b'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n',
        b"Content-Type: application/javascript+module\r\n\r\n",
        source.encode(),
        b"\r\n--" + boundary + b"--\r\n",
    ]
    return b"".join(body_parts)
