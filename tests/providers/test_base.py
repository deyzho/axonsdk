"""Tests for the provider base class / protocol."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from axon.providers.base import IAxonProvider
from axon.providers.ionet import IoNetProvider
from axon.types import DeploymentConfig, RuntimeType


def test_provider_is_abstract() -> None:
    """IAxonProvider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        IAxonProvider()  # type: ignore[abstract]


def test_ionet_provider_name() -> None:
    provider = IoNetProvider()
    assert provider.name == "ionet"


@pytest.mark.asyncio
async def test_ionet_estimate_returns_cost() -> None:
    provider = IoNetProvider()
    config = DeploymentConfig(
        name="test",
        entry_point="src/main.py",
        runtime=RuntimeType.NODEJS,
    )
    estimate = await provider.estimate(config)
    assert estimate.provider == "ionet"
    assert estimate.token == "IO"
    assert estimate.amount > 0
