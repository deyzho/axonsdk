"""Tests for AxonRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from axon.exceptions import AxonError
from axon.router import AxonRouter, CircuitBreaker, CircuitState
from axon.types import CostEstimate, RoutingStrategy


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    assert cb.state == CircuitState.CLOSED

    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # Not yet

    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_router_connect_tolerates_provider_failure() -> None:
    router = AxonRouter(providers=["ionet"], secret_key="test_key")
    router._slots["ionet"].provider.connect = AsyncMock(side_effect=Exception("Connection refused"))

    await router.connect()  # Should not raise

    # One failure recorded but circuit stays closed until threshold (5) is reached
    assert router._slots["ionet"].circuit.failure_count == 1
    assert router._slots["ionet"].circuit.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_router_raises_when_all_providers_unavailable() -> None:
    router = AxonRouter(providers=["ionet"], secret_key="test_key")
    router._slots["ionet"].circuit.record_failure()
    router._slots["ionet"].circuit.record_failure()
    router._slots["ionet"].circuit.record_failure()
    router._slots["ionet"].circuit.record_failure()
    router._slots["ionet"].circuit.record_failure()

    with pytest.raises(AxonError, match="All providers are unavailable"):
        router._select_provider()


# ─── COST routing ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cost_strategy_selects_cheapest_provider() -> None:
    """COST routing must pick the provider with the lowest cached_usd_per_hour."""
    router = AxonRouter(
        providers=["ionet", "akash"],
        secret_key="test_key",
        strategy=RoutingStrategy.COST,
    )
    # Seed cached estimates directly — ionet expensive, akash cheap
    router._slots["ionet"].cached_usd_per_hour = 2.50
    router._slots["akash"].cached_usd_per_hour = 0.30

    selected = router._select_provider()
    assert selected.provider.name == "akash"


@pytest.mark.asyncio
async def test_cost_strategy_reverses_when_prices_flip() -> None:
    """If ionet becomes cheaper, COST routing should prefer it."""
    router = AxonRouter(
        providers=["ionet", "akash"],
        secret_key="test_key",
        strategy=RoutingStrategy.COST,
    )
    router._slots["ionet"].cached_usd_per_hour = 0.10
    router._slots["akash"].cached_usd_per_hour = 1.80

    selected = router._select_provider()
    assert selected.provider.name == "ionet"


@pytest.mark.asyncio
async def test_cost_strategy_treats_no_estimate_as_last_resort() -> None:
    """Provider with no cached estimate should be chosen last."""
    router = AxonRouter(
        providers=["ionet", "akash"],
        secret_key="test_key",
        strategy=RoutingStrategy.COST,
    )
    # akash has a known estimate; ionet has none
    router._slots["ionet"].cached_usd_per_hour = None
    router._slots["akash"].cached_usd_per_hour = 0.50

    selected = router._select_provider()
    assert selected.provider.name == "akash"


@pytest.mark.asyncio
async def test_cost_strategy_falls_back_when_cheapest_tripped() -> None:
    """If the cheapest provider's circuit is open, pick the next cheapest."""
    router = AxonRouter(
        providers=["ionet", "akash"],
        secret_key="test_key",
        strategy=RoutingStrategy.COST,
    )
    router._slots["ionet"].cached_usd_per_hour = 0.10   # cheapest
    router._slots["akash"].cached_usd_per_hour = 0.50

    # Trip ionet's circuit breaker
    for _ in range(5):
        router._slots["ionet"].circuit.record_failure()
    assert not router._slots["ionet"].circuit.is_available

    selected = router._select_provider()
    assert selected.provider.name == "akash"


@pytest.mark.asyncio
async def test_refresh_cost_estimates_caches_usd_per_hour() -> None:
    """_refresh_cost_estimates() stores usd_estimate in cached_usd_per_hour."""
    router = AxonRouter(providers=["ionet"], secret_key="test_key")

    mock_estimate = CostEstimate(
        provider="ionet",
        token="IO",
        amount=0.5,
        usd_estimate=0.75,
        per_hour=True,
    )
    router._slots["ionet"].provider.estimate = AsyncMock(return_value=mock_estimate)

    await router._refresh_cost_estimates()

    assert router._slots["ionet"].cached_usd_per_hour == 0.75


@pytest.mark.asyncio
async def test_refresh_cost_estimates_ignores_provider_errors() -> None:
    """If a provider's estimate() raises, cached value stays None — no crash."""
    router = AxonRouter(providers=["ionet"], secret_key="test_key")
    router._slots["ionet"].provider.estimate = AsyncMock(
        side_effect=Exception("provider unavailable")
    )

    await router._refresh_cost_estimates()  # must not raise

    assert router._slots["ionet"].cached_usd_per_hour is None


@pytest.mark.asyncio
async def test_refresh_cost_estimates_skips_none_usd() -> None:
    """If usd_estimate is None (token-only estimate), cached value stays unchanged."""
    router = AxonRouter(providers=["ionet"], secret_key="test_key")
    router._slots["ionet"].cached_usd_per_hour = 1.23  # previously cached

    mock_estimate = CostEstimate(
        provider="ionet", token="IO", amount=0.5, usd_estimate=None, per_hour=True
    )
    router._slots["ionet"].provider.estimate = AsyncMock(return_value=mock_estimate)

    await router._refresh_cost_estimates()

    # Existing cache preserved when new estimate lacks USD conversion
    assert router._slots["ionet"].cached_usd_per_hour == 1.23
