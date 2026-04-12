"""AxonRouter — multi-provider routing with circuit breaking and health monitoring."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from axon.exceptions import AxonError, ProviderError
from axon.providers import get_provider
from axon.providers.base import IAxonProvider
from axon.types import (
    CostEstimate,
    Deployment,
    DeploymentConfig,
    HealthStatus,
    Message,
    ProviderHealth,
    ProviderName,
    RoutingStrategy,
)


class CircuitState:
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Tripped — not routing to this provider
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """Per-provider circuit breaker."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._opened_at: float | None = None

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    @property
    def is_available(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self._opened_at and (time.monotonic() - self._opened_at) >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN — allow one probe


class ProviderSlot:
    """A provider plus its circuit breaker and health data."""

    def __init__(self, provider: IAxonProvider, secret_key: str) -> None:
        self.provider = provider
        self.secret_key = secret_key
        self.circuit = CircuitBreaker()
        self.health = ProviderHealth(provider=provider.name, status=HealthStatus.HEALTHY)
        self.latency_samples: list[float] = []

    @property
    def avg_latency(self) -> float:
        if not self.latency_samples:
            return float("inf")
        return sum(self.latency_samples[-10:]) / len(self.latency_samples[-10:])


class AxonRouter:
    """
    Multi-provider router with circuit breaking, health monitoring, and failover.

    Usage:
        router = AxonRouter(
            providers=["ionet", "akash"],
            secret_key="...",
            strategy=RoutingStrategy.LATENCY,
        )
        await router.connect()
        deployment = await router.deploy(config)
    """

    def __init__(
        self,
        providers: list[ProviderName],
        secret_key: str,
        strategy: RoutingStrategy = RoutingStrategy.LATENCY,
        health_check_interval: float = 30.0,
    ) -> None:
        self._slots = {
            name: ProviderSlot(get_provider(name), secret_key)
            for name in providers
        }
        self._strategy = strategy
        self._health_check_interval = health_check_interval
        self._health_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Connect to all configured providers concurrently."""
        results = await asyncio.gather(
            *[slot.provider.connect(slot.secret_key) for slot in self._slots.values()],
            return_exceptions=True,
        )
        for name, result in zip(self._slots, results):
            if isinstance(result, Exception):
                self._slots[name].circuit.record_failure()
                self._slots[name].health = ProviderHealth(
                    provider=name,
                    status=HealthStatus.UNHEALTHY,
                    error=str(result),
                )

        self._health_task = asyncio.create_task(self._health_loop())

    async def disconnect(self) -> None:
        if self._health_task:
            self._health_task.cancel()
        await asyncio.gather(
            *[slot.provider.disconnect() for slot in self._slots.values()],
            return_exceptions=True,
        )

    def _select_provider(self) -> ProviderSlot:
        """Select a provider based on the routing strategy."""
        available = [s for s in self._slots.values() if s.circuit.is_available]
        if not available:
            raise AxonError("All providers are unavailable (circuit breakers open).")

        if self._strategy == RoutingStrategy.LATENCY:
            return min(available, key=lambda s: s.avg_latency)
        elif self._strategy == RoutingStrategy.COST:
            # Sort by estimated cost — requires last estimate cache (simplified)
            return available[0]
        elif self._strategy == RoutingStrategy.ROUND_ROBIN:
            # Simple round-robin using index
            slot = available[0]
            # Rotate by moving the first to the end
            names = list(self._slots.keys())
            available_names = [s.provider.name for s in available]
            return available[available_names.index(available[0].provider.name)]
        else:  # FAILOVER
            return available[0]

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        """Deploy to the best available provider, with automatic failover."""
        available = [s for s in self._slots.values() if s.circuit.is_available]
        last_error: Exception | None = None

        for slot in available:
            try:
                start = time.monotonic()
                deployment = await slot.provider.deploy(config)
                slot.latency_samples.append((time.monotonic() - start) * 1000)
                slot.circuit.record_success()
                return deployment
            except Exception as exc:
                slot.circuit.record_failure()
                last_error = exc

        raise AxonError(f"All providers failed. Last error: {last_error}")

    async def estimate_all(self, config: DeploymentConfig) -> list[CostEstimate]:
        """Get cost estimates from all available providers."""
        results = await asyncio.gather(
            *[s.provider.estimate(config) for s in self._slots.values()],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, CostEstimate)]

    def on_message(self, handler: Callable[[Message], None]) -> Callable[[], None]:
        """Register a message handler across all providers."""
        unsubscribers = [s.provider.on_message(handler) for s in self._slots.values()]
        return lambda: [u() for u in unsubscribers]

    def health_report(self) -> dict[ProviderName, ProviderHealth]:
        """Return health status for all providers."""
        return {name: slot.health for name, slot in self._slots.items()}

    async def _health_loop(self) -> None:
        """Background task: periodically probe provider health."""
        while True:
            await asyncio.sleep(self._health_check_interval)
            for slot in self._slots.values():
                try:
                    slot.health = await slot.provider.health()
                except Exception as exc:
                    slot.health = ProviderHealth(
                        provider=slot.provider.name,
                        status=HealthStatus.UNHEALTHY,
                        error=str(exc),
                    )

    async def __aenter__(self) -> "AxonRouter":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()
