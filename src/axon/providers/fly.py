"""Fly.io provider implementation — Fly Machines."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
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

_FLY_API = "https://api.machines.dev/v1"

# Fly.io Machine pricing (USD/hour) for shared CPU
_PRICING = {
    "shared-cpu-1x": 0.0101,
    "shared-cpu-2x": 0.0202,
    "performance-1x": 0.0625,
    "performance-2x": 0.1250,
    "default": 0.0101,
}


class FlyProvider(IAxonProvider):
    """
    Fly.io provider — Fly Machines (fast-booting Docker containers).

    Deploys workloads to Fly.io's global edge infrastructure.
    Fly Machines boot in ~300ms and are placed close to users.

    Requires:
        FLY_API_TOKEN  — Fly.io auth token (flyctl auth token)
        FLY_APP_NAME   — Fly app name (must exist: flyctl apps create <name>)
        FLY_ORG        — Fly.io organisation slug (default: personal)

    Optional:
        FLY_REGION     — Primary region (default: iad)
        FLY_IMAGE      — Docker image (default: flyio/hellofly:latest)
    """

    def __init__(self) -> None:
        self._api_token: str | None = None
        self._app_name: str | None = None
        self._org: str = "personal"
        self._region: str = "iad"
        self._client: httpx.AsyncClient | None = None
        self._connected: bool = False
        self._machine_endpoints: dict[str, str] = {}
        self._message_handlers: list[Callable[[Message], None]] = []

    @property
    def name(self) -> str:
        return "fly"

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self, secret_key: str) -> None:
        """
        Authenticate with the Fly.io Machines API using FLY_API_TOKEN.
        """
        token = os.environ.get("FLY_API_TOKEN") or secret_key
        app_name = os.environ.get("FLY_APP_NAME", "")

        if not token:
            raise AuthError(
                "Fly.io API token required. Set FLY_API_TOKEN or run `axon auth fly`.\n"
                "Get your token with: flyctl auth token"
            )
        if not app_name:
            raise AuthError(
                "FLY_APP_NAME required. Create an app first:\n"
                "  flyctl apps create <name> --org <org>"
            )

        self._api_token = token
        self._app_name = app_name
        self._org = os.environ.get("FLY_ORG", "personal")
        self._region = os.environ.get("FLY_REGION", "iad")

        self._client = httpx.AsyncClient(
            base_url=_FLY_API,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

        # Validate by listing apps
        try:
            resp = await self._client.get(f"/apps/{app_name}")
            if resp.status_code == 404:
                raise AuthError(
                    f"Fly app '{app_name}' not found. Create it with:\n"
                    f"  flyctl apps create {app_name} --org {self._org}"
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise AuthError("FLY_API_TOKEN is invalid or expired. Regenerate with: flyctl auth token") from exc
            raise ProviderError("fly", f"Fly.io API error: {exc.response.status_code}") from exc

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
        Launch Fly Machine(s) for the given workload.

        Each Machine is a fast-booting Docker container. The image must
        already be pushed to a registry accessible by Fly.io (e.g., registry.fly.io).

        Set config.metadata['image'] to override the default image.
        """
        if not self._client or not self._connected:
            raise ProviderError("fly", "Not connected. Call connect() first.")

        image = config.metadata.get(
            "image",
            os.environ.get("FLY_IMAGE", "flyio/hellofly:latest"),
        )
        vm_size = config.metadata.get("vm_size", "shared-cpu-1x")

        env = _filter_env(config.env)
        machine_ids: list[str] = []
        first_endpoint: str | None = None

        for i in range(config.replicas):
            machine_config = {
                "config": {
                    "image": image,
                    "env": env,
                    "services": [{
                        "ports": [{"port": 443, "handlers": ["tls", "http"]},
                                  {"port": 80, "handlers": ["http"]}],
                        "protocol": "tcp",
                        "internal_port": 8000,
                    }],
                    "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": config.memory_mb},
                    "restart": {"policy": "always"},
                },
                "region": self._region,
                "name": f"{_sanitise_name(config.name)}-{i}",
            }

            try:
                resp = await self._client.post(
                    f"/apps/{self._app_name}/machines",
                    json=machine_config,
                    timeout=60.0,
                )
                resp.raise_for_status()
                machine = resp.json()
                machine_id = machine.get("id", "")
                machine_ids.append(machine_id)

                if not first_endpoint and machine_id:
                    first_endpoint = f"https://{self._app_name}.fly.dev"
                    self._machine_endpoints[machine_id] = first_endpoint

            except httpx.HTTPStatusError as exc:
                raise DeploymentError(
                    "fly",
                    f"Machine launch failed (HTTP {exc.response.status_code}): {exc.response.text}",
                ) from exc

        deployment_id = machine_ids[0] if machine_ids else config.name
        if deployment_id not in self._machine_endpoints and first_endpoint:
            self._machine_endpoints[deployment_id] = first_endpoint

        return Deployment(
            id=deployment_id,
            name=config.name,
            provider="fly",
            status="active" if machine_ids else "pending",
            created_at=datetime.now(timezone.utc),
            endpoint=first_endpoint,
            metadata={
                "app_name": self._app_name,
                "machine_ids": machine_ids,
                "region": self._region,
                "image": image,
                "vm_size": vm_size,
            },
        )

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------

    async def send(self, processor_id: str, payload: Any) -> None:
        """
        POST a payload to the Fly app's public endpoint (/message).

        All Machines in the same app share the {app_name}.fly.dev domain;
        Fly's proxy load-balances across healthy machines.
        """
        endpoint = self._machine_endpoints.get(processor_id)
        if not endpoint:
            # Fall back to app domain
            if self._app_name:
                endpoint = f"https://{self._app_name}.fly.dev"
            else:
                raise ProviderError(
                    "fly", f"No endpoint for Machine {processor_id}. Did you deploy first?"
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
            raise ProviderError("fly", "Not connected.")
        resp = await self._client.get(f"/apps/{self._app_name}/machines")
        if not resp.is_success:
            return []
        machines = resp.json()
        return [
            Deployment(
                id=m.get("id", ""),
                name=m.get("name", m.get("id", "")),
                provider="fly",
                status="active" if m.get("state") == "started" else "stopped",
                created_at=datetime.fromisoformat(
                    m["created_at"].replace("Z", "+00:00")
                ) if "created_at" in m else datetime.now(timezone.utc),
                endpoint=self._machine_endpoints.get(m.get("id", "")),
                metadata={
                    "app_name": self._app_name,
                    "region": m.get("region"),
                    "image": m.get("config", {}).get("image"),
                },
            )
            for m in machines
        ]

    async def health(self) -> ProviderHealth:
        """Check Fly.io Machines API reachability."""
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("https://api.fly.io/healthcheck")
            latency_ms = (time.monotonic() - start) * 1000
            status = HealthStatus.HEALTHY if resp.is_success else HealthStatus.DEGRADED
            return ProviderHealth(provider="fly", status=status, latency_ms=latency_ms)
        except Exception as exc:
            return ProviderHealth(provider="fly", status=HealthStatus.UNHEALTHY, error=str(exc))

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        vm_size = config.metadata.get("vm_size", "shared-cpu-1x")
        hourly = _PRICING.get(vm_size, _PRICING["default"])
        duration_h = config.timeout_ms / 3_600_000
        total = hourly * duration_h * config.replicas
        return CostEstimate(
            provider="fly",
            token="USD",
            amount=total,
            usd_estimate=total,
            per_hour=True,
            breakdown={
                "vm_size": vm_size,
                "hourly_rate": hourly,
                "replicas": float(config.replicas),
            },
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_MNEMONIC", "_PRIVATE_KEY")


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not any(k.upper().endswith(s) for s in _SECRET_SUFFIXES)}


def _sanitise_name(name: str) -> str:
    return re.sub(r"[^a-z0-9\-]", "-", name.lower())[:50].strip("-") or "axon-machine"
