"""Abstract base class / Protocol for all Axon providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from axon.types import (
    CostEstimate,
    Deployment,
    DeploymentConfig,
    Message,
    ProviderHealth,
    ProviderName,
)


class IAxonProvider(ABC):
    """
    Abstract interface that every provider must implement.

    All I/O methods are async to support both HTTP and WebSocket transports
    without blocking the event loop.
    """

    @property
    @abstractmethod
    def name(self) -> ProviderName:
        """Return the canonical provider name."""

    @abstractmethod
    async def connect(self, secret_key: str) -> None:
        """Authenticate and establish a session with the provider."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Cleanly close the provider connection."""

    @abstractmethod
    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """Bundle and deploy a workload. Returns the live deployment."""

    @abstractmethod
    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        """Return a cost estimate before committing to a deployment."""

    @abstractmethod
    async def list_deployments(self) -> list[Deployment]:
        """Return all active deployments for the authenticated account."""

    @abstractmethod
    async def send(self, processor_id: str, payload: Any) -> None:
        """Send a payload to a running processor."""

    @abstractmethod
    def on_message(self, handler: Callable[[Message], None]) -> Callable[[], None]:
        """
        Register a message handler. Returns an unsubscribe callable.

        Usage:
            unsubscribe = provider.on_message(lambda msg: print(msg))
            # later:
            unsubscribe()
        """

    async def health(self) -> ProviderHealth:
        """Return current health metrics. Override for richer data."""
        from axon.types import HealthStatus
        return ProviderHealth(provider=self.name, status=HealthStatus.HEALTHY)

    async def __aenter__(self) -> "IAxonProvider":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()
