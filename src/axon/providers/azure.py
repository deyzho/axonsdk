"""Azure provider implementation — Container Instances and Azure Functions."""

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


class AzureProvider(IAxonProvider):
    """
    Azure cloud provider — Container Instances (ACI) and Azure Functions.

    Defaults to ACI. Set ``metadata.service = "functions"`` for Azure Functions.

    Requires:
        AZURE_SUBSCRIPTION_ID  — Azure subscription ID
        AZURE_RESOURCE_GROUP   — Resource group name (default: axon-rg)
        AZURE_TENANT_ID        — Azure AD tenant ID
        AZURE_CLIENT_ID        — Service principal client ID
        AZURE_CLIENT_SECRET    — Service principal client secret
        AZURE_REGION           — Region (default: eastus)
    """

    def __init__(self) -> None:
        self._subscription_id: str = ""
        self._resource_group: str = "axon-rg"
        self._region: str = "eastus"
        self._credential: Any | None = None   # azure.identity credential
        self._connected: bool = False
        self._container_endpoints: dict[str, str] = {}
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> ProviderName:
        return "azure"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Authenticate using a service principal (ClientSecretCredential) or
        DefaultAzureCredential for managed identity / CLI auth.
        """
        try:
            from azure.identity import ClientSecretCredential, DefaultAzureCredential
        except ImportError as exc:
            raise ProviderError(
                "azure",
                "azure-identity is required. Install with: pip install axonsdk-py[azure]",
            ) from exc

        self._subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
        self._resource_group = os.environ.get("AZURE_RESOURCE_GROUP", "axon-rg")
        self._region = os.environ.get("AZURE_REGION", "eastus")

        if not self._subscription_id:
            raise AuthError(
                "AZURE_SUBSCRIPTION_ID required. Set it in your .env or run `axon auth azure`."
            )

        tenant_id = os.environ.get("AZURE_TENANT_ID")
        client_id = os.environ.get("AZURE_CLIENT_ID")
        client_secret = os.environ.get("AZURE_CLIENT_SECRET")

        if tenant_id and client_id and client_secret:
            self._credential = ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
            )
        else:
            # Fall back to DefaultAzureCredential (managed identity, CLI, env, etc.)
            self._credential = DefaultAzureCredential()

        # Validate by getting a token
        try:
            assert self._credential is not None
            cred = self._credential
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: cred.get_token("https://management.azure.com/.default"),
            )
        except Exception as exc:
            raise AuthError(f"Azure authentication failed: {exc}") from exc

        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._credential = None

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        if not self._connected:
            raise ProviderError("azure", "Not connected. Call connect() first.")

        service = config.metadata.get("service", "aci")
        if service == "functions":
            return await self._deploy_functions(config)
        return await self._deploy_aci(config)

    async def _deploy_aci(self, config: DeploymentConfig) -> Deployment:
        """Deploy a container to Azure Container Instances."""
        try:
            from azure.mgmt.containerinstance import ContainerInstanceManagementClient
            from azure.mgmt.containerinstance.models import (
                Container,
                ContainerGroup,
                ContainerPort,
                EnvironmentVariable,
                IpAddress,
                OperatingSystemTypes,
                Port,
                ResourceRequests,
                ResourceRequirements,
            )
        except ImportError as exc:
            raise ProviderError(
                "azure",
                "azure-mgmt-containerinstance required. "
                "Install with: pip install axonsdk-py[azure]",
            ) from exc

        container_name = _sanitise_name(config.name)
        image = config.metadata.get(
            "image",
            "mcr.microsoft.com/azuredocs/aci-helloworld",  # placeholder
        )

        env_vars = [
            EnvironmentVariable(name=k, value=v)
            for k, v in _filter_env(config.env).items()
        ]

        cpu = float(config.metadata.get("cpu", 1.0))
        mem_gb = config.memory_mb / 1024

        container = Container(
            name=container_name,
            image=image,
            resources=ResourceRequirements(
                requests=ResourceRequests(memory_in_gb=mem_gb, cpu=cpu)
            ),
            ports=[ContainerPort(port=8000)],
            environment_variables=env_vars,
        )

        group = ContainerGroup(
            location=self._region,
            containers=[container] * config.replicas,
            os_type=OperatingSystemTypes.LINUX,
            ip_address=IpAddress(ports=[Port(port=8000)], type="Public"),
        )

        aci_client = ContainerInstanceManagementClient(self._credential, self._subscription_id)  # type: ignore[arg-type]

        def _create() -> Any:
            return aci_client.container_groups.begin_create_or_update(
                self._resource_group, container_name, group
            ).result()

        result = await asyncio.get_event_loop().run_in_executor(None, _create)
        ip = result.ip_address.ip if result.ip_address else None
        endpoint = f"https://{ip}:8000" if ip else None
        if endpoint:
            self._container_endpoints[container_name] = endpoint

        return Deployment(
            id=container_name,
            name=config.name,
            provider="azure",
            status="active" if ip else "pending",
            created_at=datetime.now(UTC),
            endpoint=endpoint,
            metadata={
                "service": "aci",
                "resource_group": self._resource_group,
                "region": self._region,
                "image": image,
            },
        )

    async def _deploy_functions(self, config: DeploymentConfig) -> Deployment:
        """Deploy a Python function to Azure Functions."""
        import httpx

        func_name = _sanitise_name(config.name)
        entry = Path(config.entry_point)
        if not entry.exists():
            raise DeploymentError("azure", f"Entry point not found: {entry}")

        # Build function zip
        zip_path = _build_functions_zip(entry, config)

        try:
            assert self._credential is not None
            cred = self._credential
            token = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: cred.get_token(
                    "https://management.azure.com/.default"
                ).token,
            )

            # Assumes Function App already exists — deploy zip via Kudu API
            app_name = config.metadata.get("function_app_name", func_name)
            deploy_url = (
                f"https://{app_name}.scm.azurewebsites.net/api/zipdeploy"
            )

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    deploy_url,
                    content=zip_path.read_bytes(),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/zip",
                    },
                )
                resp.raise_for_status()

            invoke_url = f"https://{app_name}.azurewebsites.net/api/{func_name}"
            self._container_endpoints[func_name] = invoke_url

            return Deployment(
                id=func_name,
                name=config.name,
                provider="azure",
                status="active",
                created_at=datetime.now(UTC),
                endpoint=invoke_url,
                metadata={"service": "functions", "app_name": app_name, "region": self._region},
            )
        except Exception as exc:
            raise DeploymentError("azure", f"Azure Functions deploy failed: {exc}") from exc
        finally:
            zip_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """POST payload to an ACI container or Azure Functions endpoint."""
        import httpx

        endpoint = self._container_endpoints.get(processor_id)
        if not endpoint:
            raise ProviderError("azure", f"No endpoint for {processor_id}. Did you deploy first?")

        assert self._credential is not None
        cred = self._credential
        token = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: cred.get_token("https://management.azure.com/.default").token,
        )

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
            raise ProviderError("azure", "Not connected.")
        try:
            from azure.mgmt.containerinstance import ContainerInstanceManagementClient
            aci_client = ContainerInstanceManagementClient(self._credential, self._subscription_id)  # type: ignore[arg-type]
            groups = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: list(
                    aci_client.container_groups.list_by_resource_group(self._resource_group)
                ),
            )
            return [
                Deployment(
                    id=g.name or "",
                    name=g.name or "",
                    provider="azure",
                    status="active" if g.provisioning_state == "Succeeded" else "pending",
                    created_at=datetime.now(UTC),
                    endpoint=self._container_endpoints.get(g.name or ""),
                    metadata={"service": "aci", "region": self._region},
                )
                for g in groups
            ]
        except Exception:
            return []

    async def teardown(self, deployment_id: str) -> None:
        """Delete an ACI container group."""
        if not self._connected:
            return
        try:
            from azure.mgmt.containerinstance import ContainerInstanceManagementClient
            aci_client = ContainerInstanceManagementClient(self._credential, self._subscription_id)  # type: ignore[arg-type]
            # deployment_id may be full Azure resource ID or just container group name
            name = deployment_id.split("/")[-1] if "/" in deployment_id else deployment_id
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: aci_client.container_groups.begin_delete(
                    self._resource_group, name
                ).result(),
            )
        except Exception:
            pass  # Best effort

    async def health(self) -> ProviderHealth:
        """Probe Azure management API."""
        import httpx
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("https://management.azure.com/")
            latency_ms = (time.monotonic() - start) * 1000
            ok_codes = (200, 401, 403)
            status = HealthStatus.HEALTHY if resp.status_code in ok_codes else HealthStatus.DEGRADED
            return ProviderHealth(provider="azure", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="azure", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        duration_s = config.timeout_ms / 1000
        vcpu = float(config.metadata.get("cpu", 1.0))
        mem_gb = config.memory_mb / 1024
        pricing = await get_pricing()
        cost = (
            (pricing.azure_aci_vcpu_sec * vcpu + pricing.azure_aci_gib_sec * mem_gb)
            * duration_s * config.replicas
        )
        return CostEstimate(
            provider="azure",
            token="USD",
            amount=cost,
            usd_estimate=cost,
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
    return re.sub(r"[^a-z0-9\-]", "-", name.lower())[:63].strip("-") or "axon-container"


def _build_functions_zip(entry: Path, config: DeploymentConfig) -> Path:
    """Build Azure Functions deployment zip (Python v2 programming model)."""
    safe_env = _filter_env(config.env)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", prefix="axon-azure-", delete=False)
    tmp.close()
    zip_path = Path(tmp.name)
    source = entry.read_text(encoding="utf-8")
    env_lines = "".join(
        f'os.environ["{k}"] = {json.dumps(v)}\n' for k, v in safe_env.items()
    )
    func_code = (
        "import os, json, azure.functions as func\n\n"
        + env_lines
        + "\n"
        + source
        + "\n\n"
        "app = func.FunctionApp()\n\n"
        "@app.route(route='invoke')\n"
        "def invoke(req: func.HttpRequest) -> func.HttpResponse:\n"
        "    data = req.get_json()\n"
        "    result = handle_message(data.get('payload', data))\n"
        "    return func.HttpResponse(json.dumps(result), mimetype='application/json')\n"
    )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("function_app.py", func_code)
        zf.writestr("requirements.txt", "azure-functions\n")
        zf.writestr("host.json", json.dumps({"version": "2.0", "extensionBundle": {
            "id": "Microsoft.Azure.Functions.ExtensionBundle",
            "version": "[4.*, 5.0.0)"
        }}))
    return zip_path
