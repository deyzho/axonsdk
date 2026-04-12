"""AxonClient — single-provider interface for the Axon SDK."""

from __future__ import annotations

import os
from typing import Any, Callable

from axon.exceptions import AuthError, AxonError
from axon.providers import get_provider
from axon.providers.base import IAxonProvider
from axon.types import (
    CostEstimate,
    Deployment,
    DeploymentConfig,
    Message,
    ProviderHealth,
    ProviderName,
)


class AxonClient:
    """
    Single-provider Axon client.

    Usage:
        async with AxonClient(provider="ionet", secret_key="...") as client:
            deployment = await client.deploy(config)
            await client.send(deployment.id, {"prompt": "Hello"})

    The secret_key falls back to the AXON_SECRET_KEY environment variable.
    """

    def __init__(
        self,
        provider: ProviderName,
        secret_key: str | None = None,
    ) -> None:
        self._provider_name = provider
        self._secret_key = secret_key or os.environ.get("AXON_SECRET_KEY")
        self._provider: IAxonProvider = get_provider(provider)

    async def connect(self) -> None:
        """Connect and authenticate with the provider."""
        if not self._secret_key:
            raise AuthError(
                "Secret key required. Pass secret_key= or set AXON_SECRET_KEY in your environment."
            )
        await self._provider.connect(self._secret_key)

    async def disconnect(self) -> None:
        """Disconnect from the provider."""
        await self._provider.disconnect()

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """Deploy a workload to the configured provider."""
        return await self._provider.deploy(config)

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        """Get a cost estimate for a deployment."""
        return await self._provider.estimate(config)

    async def list_deployments(self) -> list[Deployment]:
        """List all active deployments."""
        return await self._provider.list_deployments()

    async def send(self, processor_id: str, payload: Any) -> None:
        """Send a payload to a running processor."""
        import re
        if not processor_id or not isinstance(processor_id, str):
            raise AxonError("processor_id must be a non-empty string.")
        if len(processor_id) > 512:
            raise AxonError("processor_id exceeds maximum length of 512 characters.")
        # Reject control characters, null bytes, and path traversal sequences to
        # prevent injection if the id is later embedded in a URL or shell command.
        if re.search(r'[\x00-\x1f\x7f]|\.\.[\\/]|[\\/]', processor_id):
            raise AxonError(
                "Invalid processor_id: must not contain control characters, "
                "null bytes, or path traversal sequences."
            )
        await self._provider.send(processor_id, payload)

    def on_message(self, handler: Callable[[Message], None]) -> Callable[[], None]:
        """Register a message handler. Returns an unsubscribe callable."""
        return self._provider.on_message(handler)

    async def health(self) -> ProviderHealth:
        """Return current health status of the provider."""
        return await self._provider.health()

    async def __aenter__(self) -> "AxonClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()
