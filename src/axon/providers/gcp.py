"""Google Cloud provider implementation — Cloud Run and Cloud Functions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axon.exceptions import AuthError, DeploymentError, ProviderError
from axon.pricing import get_pricing
from axon.providers.base import IAxonProvider
from axon.types import (
    CostEstimate,
    Deployment,
    DeploymentConfig,
    HealthStatus,
    Message,
    ProviderHealth,
    ProviderName,
)


class GCPProvider(IAxonProvider):
    """
    Google Cloud provider — Cloud Run (containers) and Cloud Functions (serverless).

    Defaults to Cloud Run. Set ``metadata.service = "functions"`` for Cloud Functions.

    Requires:
        GCP_PROJECT_ID         — Google Cloud project ID
        GCP_REGION             — Region (default: us-central1)
        GOOGLE_APPLICATION_CREDENTIALS — Path to service account JSON
                                          (or use Application Default Credentials)

    Optional:
        GCP_SERVICE_ACCOUNT    — Service account email for Cloud Run identity
    """

    def __init__(self) -> None:
        self._project: str = ""
        self._region: str = "us-central1"
        self._credentials: Any | None = None   # google.oauth2.credentials.Credentials
        self._connected: bool = False
        self._service_urls: dict[str, str] = {}   # service_name -> invoke URL
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> ProviderName:
        return "gcp"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Authenticate using Application Default Credentials or a service account key file.
        """
        try:
            import google.auth
            import google.auth.transport.requests
        except ImportError as exc:
            raise ProviderError(
                "gcp",
                "google-auth is required. Install with: pip install axonsdk-py[gcp]",
            ) from exc

        self._project = os.environ.get("GCP_PROJECT_ID", "")
        self._region = os.environ.get("GCP_REGION", "us-central1")

        if not self._project:
            raise AuthError(
                "GCP_PROJECT_ID required. Set it in your .env or run `axon auth gcp`."
            )

        try:
            creds, project = await asyncio.get_event_loop().run_in_executor(
                None, google.auth.default
            )
            if not self._project:
                self._project = project or ""
            self._credentials = creds
        except Exception as exc:
            raise AuthError(
                f"GCP authentication failed. Set GOOGLE_APPLICATION_CREDENTIALS "
                f"or configure Application Default Credentials: {exc}"
            ) from exc

        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._credentials = None

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """
        Deploy to Cloud Run (default) or Cloud Functions.
        """
        if not self._connected:
            raise ProviderError("gcp", "Not connected. Call connect() first.")

        service = config.metadata.get("service", "run")
        if service == "functions":
            return await self._deploy_functions(config)
        return await self._deploy_run(config)

    async def _deploy_run(self, config: DeploymentConfig) -> Deployment:
        """Deploy a container to Cloud Run via the REST API."""
        try:
            import google.auth.transport.requests
            import httpx
        except ImportError as exc:
            raise ProviderError("gcp", "Install with: pip install axonsdk-py[gcp]") from exc

        service_name = _sanitise_name(config.name)
        image = config.metadata.get(
            "image",
            f"gcr.io/{self._project}/{service_name}:latest",
        )

        env_vars = [
            {"name": k, "value": v}
            for k, v in _filter_env(config.env).items()
        ]

        # Refresh credentials token
        assert self._credentials is not None
        creds = self._credentials
        request = google.auth.transport.requests.Request()
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: creds.refresh(request)
        )
        token = creds.token

        run_url = (
            f"https://run.googleapis.com/v2/projects/{self._project}"
            f"/locations/{self._region}/services/{service_name}"
        )

        body = {
            "template": {
                "containers": [{
                    "image": image,
                    "env": env_vars,
                    "resources": {
                        "limits": {
                            "memory": f"{config.memory_mb}Mi",
                            "cpu": str(config.metadata.get("cpu", "1")),
                        }
                    },
                }],
                "scaling": {"minInstanceCount": 0, "maxInstanceCount": config.replicas},
                "timeout": f"{config.timeout_ms // 1000}s",
            },
            "ingress": "INGRESS_TRAFFIC_ALL",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            # Try PATCH (update) first, then POST (create)
            resp = await client.patch(
                run_url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 404:
                resp = await client.post(
                    f"https://run.googleapis.com/v2/projects/{self._project}"
                    f"/locations/{self._region}/services",
                    json={**body, "name": service_name},
                    headers={"Authorization": f"Bearer {token}"},
                )
            resp.raise_for_status()
            data = resp.json()

        service_url = data.get("uri", "")
        self._service_urls[service_name] = service_url

        return Deployment(
            id=service_name,
            name=config.name,
            provider="gcp",
            status="active" if service_url else "pending",
            created_at=datetime.now(UTC),
            endpoint=service_url or None,
            metadata={
                "service": "run",
                "project": self._project,
                "region": self._region,
                "image": image,
            },
        )

    async def _deploy_functions(self, config: DeploymentConfig) -> Deployment:
        """Deploy a Python/Node.js function to Cloud Functions (2nd gen)."""
        import google.auth.transport.requests
        import httpx

        func_name = _sanitise_name(config.name)
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("gcp", f"Entry point not found: {entry}")

        # Build source zip
        zip_path = _build_source_zip(entry, config)

        try:
            assert self._credentials is not None
            creds = self._credentials
            request = google.auth.transport.requests.Request()
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: creds.refresh(request)
            )
            token = creds.token

            # Upload source to GCS staging bucket
            gcs_bucket = config.metadata.get("gcs_bucket", f"{self._project}-gcf-source")
            gcs_object = f"axon/{func_name}-source.zip"

            async with httpx.AsyncClient(timeout=120.0) as client:
                # Upload zip to GCS
                upload_url = (
                    f"https://storage.googleapis.com/upload/storage/v1/b"
                    f"/{gcs_bucket}/o?uploadType=media&name={gcs_object}"
                )
                upload_resp = await client.post(
                    upload_url,
                    content=zip_path.read_bytes(),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/zip",
                    },
                )
                upload_resp.raise_for_status()

                # Create / update the Cloud Function
                runtime_map = {"nodejs": "nodejs20", "python": "python311", "docker": "python311"}
                runtime = runtime_map.get(config.runtime.value, "python311")
                entry_point = config.metadata.get("entry_point_fn", "handle_message")

                fn_url = (
                    f"https://cloudfunctions.googleapis.com/v2/projects/{self._project}"
                    f"/locations/{self._region}/functions/{func_name}"
                )
                fn_body = {
                    "name": fn_url.split("cloudfunctions.googleapis.com/v2/")[-1],
                    "buildConfig": {
                        "runtime": runtime,
                        "entryPoint": entry_point,
                        "source": {
                            "storageSource": {"bucket": gcs_bucket, "object": gcs_object}
                        },
                    },
                    "serviceConfig": {
                        "timeoutSeconds": config.timeout_ms // 1000,
                        "availableMemory": f"{config.memory_mb}M",
                        "maxInstanceCount": config.replicas,
                        "environmentVariables": _filter_env(config.env),
                    },
                }

                fn_resp = await client.patch(
                    fn_url,
                    json=fn_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if fn_resp.status_code == 404:
                    fn_resp = await client.post(
                        f"https://cloudfunctions.googleapis.com/v2/projects/{self._project}"
                        f"/locations/{self._region}/functions",
                        json={**fn_body, "functionId": func_name},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                fn_resp.raise_for_status()
                data = fn_resp.json()

            invoke_url = data.get("serviceConfig", {}).get("uri", "")
            self._service_urls[func_name] = invoke_url

            return Deployment(
                id=func_name,
                name=config.name,
                provider="gcp",
                status="active" if invoke_url else "pending",
                created_at=datetime.now(UTC),
                endpoint=invoke_url or None,
                metadata={
                    "service": "functions",
                    "project": self._project,
                    "region": self._region,
                    "runtime": runtime,
                },
            )

        except Exception as exc:
            raise DeploymentError("gcp", f"Cloud Functions deploy failed: {exc}") from exc
        finally:
            zip_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """POST payload to a Cloud Run service or Cloud Functions endpoint."""
        import google.auth.transport.requests
        import httpx

        endpoint = self._service_urls.get(processor_id)
        if not endpoint:
            raise ProviderError("gcp", f"No endpoint for {processor_id}. Did you deploy first?")

        # Refresh token for authenticated Cloud Run services
        assert self._credentials is not None
        creds = self._credentials
        request = google.auth.transport.requests.Request()
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: creds.refresh(request)
        )
        token = creds.token

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                endpoint,
                json={"payload": payload},
                headers={"Authorization": f"Bearer {token}"},
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
        if not self._connected:
            raise ProviderError("gcp", "Not connected.")
        import google.auth.transport.requests
        import httpx
        assert self._credentials is not None
        creds = self._credentials
        request = google.auth.transport.requests.Request()
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: creds.refresh(request)
        )
        token = creds.token
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://run.googleapis.com/v2/projects/{self._project}/locations/{self._region}/services",
                headers={"Authorization": f"Bearer {token}"},
            )
        if not resp.is_success:
            return []
        services = resp.json().get("services", [])
        return [
            Deployment(
                id=svc.get("name", "").split("/")[-1],
                name=svc.get("name", "").split("/")[-1],
                provider="gcp",
                status=(
                    "active"
                    if svc.get("conditions", [{}])[0].get("state") == "CONDITION_SUCCEEDED"
                    else "pending"
                ),
                created_at=datetime.now(UTC),
                endpoint=svc.get("uri"),
                metadata={"service": "run", "region": self._region},
            )
            for svc in services
        ]

    async def teardown(self, deployment_id: str) -> None:
        """Delete a Cloud Run service."""
        if not self._connected:
            return
        try:
            import google.auth.transport.requests
            import httpx
            assert self._credentials is not None
            creds = self._credentials
            request = google.auth.transport.requests.Request()
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: creds.refresh(request)
            )
            token = creds.token
            # deployment_id may be just the service name or full resource name
            if deployment_id.startswith("projects/"):
                service_name = deployment_id
            else:
                service_name = (
                    f"projects/{self._project}/locations/{self._region}/services/{deployment_id}"
                )
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.delete(
                    f"https://run.googleapis.com/v2/{service_name}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                # 200/202 = deletion started, 404 = already gone
                if resp.status_code not in (200, 202, 404):
                    resp.raise_for_status()
        except Exception:
            pass  # Best effort

    async def health(self) -> ProviderHealth:
        """Probe Cloud Run API endpoint."""
        import httpx
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("https://run.googleapis.com/$discovery/rest")
            latency_ms = (time.monotonic() - start) * 1000
            status = HealthStatus.HEALTHY if resp.is_success else HealthStatus.DEGRADED
            return ProviderHealth(provider="gcp", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="gcp", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        duration_s = config.timeout_ms / 1000
        vcpu = float(config.metadata.get("cpu", 1))
        mem_gb = config.memory_mb / 1024
        pricing = await get_pricing()
        compute = (
            (pricing.gcp_run_vcpu_sec * vcpu + pricing.gcp_run_gib_sec * mem_gb)
            * duration_s * config.replicas
        )
        return CostEstimate(
            provider="gcp",
            token="USD",
            amount=compute,
            usd_estimate=compute,
            per_hour=False,
            breakdown={"vcpu_seconds": vcpu * duration_s, "mem_gb_seconds": mem_gb * duration_s},
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_MNEMONIC", "_PRIVATE_KEY")


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in env.items()
        if not any(k.upper().endswith(s) for s in _SECRET_SUFFIXES)
    }


def _sanitise_name(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9\-]", "-", name.lower())[:49].strip("-") or "axon-service"


def _build_source_zip(entry: Path, config: DeploymentConfig) -> Path:
    safe_env = _filter_env(config.env)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", prefix="axon-gcp-", delete=False)
    tmp.close()
    zip_path = Path(tmp.name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        source = entry.read_text(encoding="utf-8")
        env_lines = "".join(
            f'os.environ["{k}"] = {json.dumps(v)}\n' for k, v in safe_env.items()
        )
        wrapper = (
            "import os, json\n\n"
            + env_lines
            + "\n"
            + source
            + "\n\n"
            "def handle_message_http(request):\n"
            "    data = request.get_json(silent=True) or {}\n"
            "    result = handle_message(data.get('payload', data))\n"
            "    return json.dumps(result), 200, {'Content-Type': 'application/json'}\n"
        )
        zf.writestr("main.py", wrapper)
        zf.writestr("requirements.txt", "functions-framework==3.*\n")
    return zip_path
