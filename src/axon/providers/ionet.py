"""io.net provider implementation."""

from __future__ import annotations

import json
import os
import tempfile
import time
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

# Maximum response body size (4 MiB — larger than other providers for GPU output)
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class IoNetProvider(IAxonProvider):
    """
    io.net GPU cluster provider.

    Deploys Python/Node.js workloads to io.net GPU clusters (A100, H100, RTX).
    Uses IPFS for bundle distribution and the io.net Jobs API for scheduling.

    Requires:
        IONET_API_KEY    — API key from console.io.net
        IONET_IPFS_URL   — IPFS API endpoint for bundle uploads
        IONET_CLUSTER_ID — (optional) specific cluster; auto-selects cheapest if omitted
    """

    BASE_URL = "https://api.io.net/v1"
    # USD/hour estimates per GPU tier
    _GPU_PRICING: dict[str, float] = {
        "A100": 0.85,
        "H100": 2.50,
        "RTX4090": 0.40,
        "RTX3090": 0.20,
        "default": 0.40,
    }

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._api_key: str | None = None
        self._cluster_id: str | None = None
        # Maps job_id → worker_endpoint for active deployments
        self._endpoints: dict[str, str] = {}
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "ionet"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """Authenticate with io.net using IONET_API_KEY (falls back to secret_key)."""
        api_key = os.environ.get("IONET_API_KEY") or secret_key
        if not api_key:
            raise AuthError(
                "io.net API key required. Set IONET_API_KEY or run `axon auth ionet`."
            )
        self._api_key = api_key
        self._cluster_id = os.environ.get("IONET_CLUSTER_ID")
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        # Validate credentials
        try:
            resp = await self._client.get("/user/me")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await self._client.aclose()
            self._client = None
            raise AuthError(f"io.net auth failed (HTTP {exc.response.status_code})") from exc
        except httpx.RequestError as exc:
            await self._client.aclose()
            self._client = None
            raise ProviderError("ionet", f"Could not reach io.net API: {exc}") from exc

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy a workload to an io.net GPU cluster.

        Steps:
          1. Bundle the entry point file
          2. Upload bundle to IPFS
          3. Select (or use pinned) cluster
          4. Submit job to io.net Jobs API
          5. Return Deployment with worker endpoints
        """
        if not self._client:
            raise ProviderError("ionet", "Not connected. Call connect() first.")

        # 1. Bundle
        bundle_path = self._bundle(config)

        try:
            # 2. Upload to IPFS
            bundle_cid = await self._upload_ipfs(bundle_path)

            # 3. Select cluster
            cluster_id = self._cluster_id or await self._select_cluster()

            # 4. Submit job
            payload = {
                "cluster_id": cluster_id,
                "runtime": config.runtime.value,
                "bundle_cid": bundle_cid,
                "replicas": config.replicas,
                "duration_ms": config.timeout_ms,
                "environment": config.env,
                "name": config.name,
            }
            resp = await self._client.post("/jobs", json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()

            job_id: str = data["job_id"]
            worker_endpoints: list[str] = data.get("worker_endpoints", [])
            if worker_endpoints:
                self._endpoints[job_id] = worker_endpoints[0]

            return Deployment(
                id=job_id,
                name=config.name,
                provider="ionet",
                status="active" if worker_endpoints else "pending",
                created_at=datetime.now(timezone.utc),
                endpoint=worker_endpoints[0] if worker_endpoints else None,
                metadata={
                    "cluster_id": cluster_id,
                    "bundle_cid": bundle_cid,
                    "worker_endpoints": worker_endpoints,
                },
            )

        except httpx.HTTPStatusError as exc:
            raise DeploymentError(
                "ionet", f"Job submission failed (HTTP {exc.response.status_code}): {exc.response.text}"
            ) from exc
        finally:
            # Clean up temp bundle
            bundle_path.unlink(missing_ok=True)

    def _bundle(self, config: DeploymentConfig) -> Path:
        """
        Write the entry point to a staging temp file.

        For Python workloads this is the entry file as-is (io.net runs Python natively).
        For Node.js workloads the file is passed through unchanged — a proper bundler
        (esbuild/pyinstaller) integration can be layered in here later.
        """
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("ionet", f"Entry point not found: {entry}")

        # Inject safe environment variables into a wrapper
        safe_env = _filter_env(config.env)
        tmp = tempfile.NamedTemporaryFile(
            suffix=entry.suffix,
            prefix="axon-ionet-",
            delete=False,
        )
        source = entry.read_text(encoding="utf-8")
        if config.runtime.value == "nodejs":
            preamble = "".join(
                f'process.env["{k}"] = {json.dumps(v)};\n' for k, v in safe_env.items()
            )
        else:
            preamble = "".join(
                f'import os; os.environ["{k}"] = {json.dumps(v)}\n' for k, v in safe_env.items()
            )
        tmp.write((preamble + source).encode())
        tmp.close()
        return Path(tmp.name)

    async def _upload_ipfs(self, bundle_path: Path) -> str:
        """Upload bundle to IPFS and return the CID."""
        ipfs_url = os.environ.get("IONET_IPFS_URL") or os.environ.get("AKASH_IPFS_URL", "")
        if not ipfs_url:
            # No IPFS configured — use a local marker; io.net can serve from local store
            return "local"

        _validate_ipfs_url(ipfs_url)
        api_key = os.environ.get("IONET_IPFS_API_KEY") or os.environ.get("AKASH_IPFS_API_KEY")
        headers = {"Authorization": f"Basic {api_key}"} if api_key else {}

        async with httpx.AsyncClient(timeout=120.0) as ipfs_client:
            with bundle_path.open("rb") as fh:
                resp = await ipfs_client.post(
                    f"{ipfs_url.rstrip('/')}/api/v0/add",
                    files={"file": fh},
                    headers=headers,
                )
            resp.raise_for_status()
            data = resp.json()
            cid: str = data.get("Hash") or data.get("cid", {}).get("/", "")
            if not cid:
                raise DeploymentError("ionet", f"IPFS upload returned no CID: {data}")
            return cid

    async def _select_cluster(self) -> str:
        """Auto-select the cheapest available cluster."""
        if not self._client:
            raise ProviderError("ionet", "Not connected.")
        resp = await self._client.get("/clusters")
        resp.raise_for_status()
        clusters: list[dict[str, Any]] = resp.json().get("clusters", [])
        if not clusters:
            raise DeploymentError("ionet", "No clusters available.")
        # Sort by hourly price, pick cheapest
        clusters.sort(key=lambda c: c.get("price_per_hour_usd", 9999))
        return clusters[0]["cluster_id"]

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        Send a payload to a running io.net job worker.

        processor_id is the job_id returned by deploy().
        Response is dispatched to all registered on_message handlers.
        """
        if not self._client:
            raise ProviderError("ionet", "Not connected.")

        endpoint = self._endpoints.get(processor_id)
        if not endpoint:
            # Try to fetch it from the API
            resp = await self._client.get(f"/jobs/{processor_id}")
            resp.raise_for_status()
            data = resp.json()
            workers: list[str] = data.get("worker_endpoints", [])
            if not workers:
                raise ProviderError("ionet", f"No worker endpoints for job {processor_id}")
            endpoint = workers[0]
            self._endpoints[processor_id] = endpoint

        _validate_endpoint_url(endpoint)

        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=60.0,
        ) as worker_client:
            resp = await worker_client.post(
                f"{endpoint.rstrip('/')}/message",
                json={"payload": payload},
            )
            resp.raise_for_status()

            if len(resp.content) > _MAX_RESPONSE_BYTES:
                raise ProviderError("ionet", "Response exceeded 4 MiB limit")

            try:
                result = resp.json()
            except Exception:
                result = resp.text

        msg = Message(
            processor_id=processor_id,
            payload=result,
        )
        for handler in list(self._message_handlers):
            handler(msg)

    def on_message(self, handler: Callable[[Message], None]) -> Callable[[], None]:
        self._message_handlers.append(handler)
        return lambda: self._message_handlers.remove(handler)

    # ------------------------------------------------------------------
    # Listings & health
    # ------------------------------------------------------------------

    async def list_deployments(self) -> list[Deployment]:
        if not self._client:
            raise ProviderError("ionet", "Not connected.")
        resp = await self._client.get("/jobs")
        resp.raise_for_status()
        jobs: list[dict[str, Any]] = resp.json().get("jobs", [])
        result = []
        for job in jobs:
            workers = job.get("worker_endpoints", [])
            result.append(
                Deployment(
                    id=job["job_id"],
                    name=job.get("name", job["job_id"]),
                    provider="ionet",
                    status=_map_status(job.get("status", "")),
                    created_at=_parse_ts(job.get("created_at")),
                    endpoint=workers[0] if workers else None,
                    metadata={"cluster_id": job.get("cluster_id"), "worker_endpoints": workers},
                )
            )
        return result

    async def health(self) -> ProviderHealth:
        if not self._client:
            return ProviderHealth(provider="ionet", status=HealthStatus.UNHEALTHY, error="Not connected")
        try:
            start = time.monotonic()
            resp = await self._client.get("/health")
            latency_ms = (time.monotonic() - start) * 1000
            status = HealthStatus.HEALTHY if resp.is_success else HealthStatus.DEGRADED
            return ProviderHealth(provider="ionet", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="ionet", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        # Try to fetch live cluster pricing
        gpu_tier = config.metadata.get("gpu_tier", "default")
        usd_per_hour = self._GPU_PRICING.get(gpu_tier, self._GPU_PRICING["default"])
        io_per_usd = 10.0  # approximate IO/USD rate
        return CostEstimate(
            provider="ionet",
            token="IO",
            amount=usd_per_hour * io_per_usd * config.replicas,
            usd_estimate=usd_per_hour * config.replicas,
            per_hour=True,
            breakdown={"gpu": usd_per_hour, "replicas": float(config.replicas)},
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_MNEMONIC", "_PRIVATE_KEY")


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    """Strip secret-looking variables before injecting into bundles."""
    return {
        k: v for k, v in env.items()
        if not any(k.upper().endswith(s) for s in _SECRET_SUFFIXES)
    }


def _validate_ipfs_url(url: str) -> None:
    assert_safe_url(url, "ionet", "IPFS URL")


def _validate_endpoint_url(url: str) -> None:
    assert_safe_url(url, "ionet", "Worker endpoint")


def _map_status(raw: str) -> str:
    mapping = {"running": "active", "pending": "pending", "stopped": "stopped", "failed": "failed"}
    return mapping.get(raw.lower(), "pending")


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
