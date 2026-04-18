"""AxonRouter — multi-provider routing with circuit breaking and health monitoring."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from axon.exceptions import AxonError
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

# Reference workload used to fetch comparable cost estimates across providers.
# All providers are queried with the same spec so costs are apples-to-apples.
_REFERENCE_CONFIG = DeploymentConfig(
    name="cost-probe",
    entry_point="index.js",
    memory_mb=512,
    replicas=1,
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
        # Last-known USD/hr cost for the reference workload (_REFERENCE_CONFIG).
        # None until the first successful estimate(); providers with no estimate
        # are treated as most expensive when COST routing is active.
        self.cached_usd_per_hour: float | None = None

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
        self._health_task: asyncio.Task[None] | None = None
        self._rr_index: int = 0

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

        # Seed cost estimates so COST routing is functional from the first request.
        await self._refresh_cost_estimates()
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
            # Sort by last-known USD/hr estimate for the reference workload.
            # Estimates are fetched at connect() and refreshed every health-check
            # cycle. Providers with no cached estimate are treated as most expensive
            # (float("inf")) so they are used only as a last resort.
            return min(
                available,
                key=lambda s: s.cached_usd_per_hour
                if s.cached_usd_per_hour is not None
                else float("inf"),
            )
        elif self._strategy == RoutingStrategy.ROUND_ROBIN:
            idx = self._rr_index % len(available)
            self._rr_index += 1
            return available[idx]
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

        def _unsubscribe() -> None:
            for u in unsubscribers:
                u()

        return _unsubscribe

    def health_report(self) -> dict[ProviderName, ProviderHealth]:
        """Return health status for all providers."""
        return {name: slot.health for name, slot in self._slots.items()}

    async def _refresh_cost_estimates(self) -> None:
        """Fetch cost estimates from all providers and cache the USD/hr rate.

        Uses a fixed reference workload (_REFERENCE_CONFIG) so estimates are
        directly comparable across providers. Failures are silently ignored —
        a provider with no cached estimate is sorted last under COST routing.
        """
        estimates = await asyncio.gather(
            *[slot.provider.estimate(_REFERENCE_CONFIG) for slot in self._slots.values()],
            return_exceptions=True,
        )
        for slot, result in zip(self._slots.values(), estimates):
            if isinstance(result, CostEstimate) and result.usd_estimate is not None:
                slot.cached_usd_per_hour = result.usd_estimate

    async def _health_loop(self) -> None:
        """Background task: periodically probe provider health and refresh cost estimates."""
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
            # Refresh cost estimates so COST routing stays accurate over time.
            await self._refresh_cost_estimates()

    async def __aenter__(self) -> AxonRouter:
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()
