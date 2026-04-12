"""Tests for AxonRouter."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from axon.router import AxonRouter, CircuitBreaker, CircuitState
from axon.exceptions import AxonError


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
