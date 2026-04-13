"""Tests for the CloudflareProvider."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from axon.exceptions import AuthError, ProviderError
from axon.providers.cloudflare import CloudflareProvider
from axon.types import CostEstimate, DeploymentConfig, RuntimeType


# ---------------------------------------------------------------------------
# test_connect_requires_token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_requires_token() -> None:
    """connect() raises AuthError when CF_API_TOKEN is absent and secret_key is empty."""
    provider = CloudflareProvider()
    with patch.dict("os.environ", {"CF_ACCOUNT_ID": "acc123"}, clear=True):
        # Env only has CF_ACCOUNT_ID; no token provided
        with pytest.raises(AuthError, match="CF_API_TOKEN"):
            await provider.connect("")


# ---------------------------------------------------------------------------
# test_connect_requires_account_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_requires_account_id() -> None:
    """connect() raises AuthError when CF_ACCOUNT_ID is absent."""
    provider = CloudflareProvider()
    # Provide a token so we pass the token check, but omit account ID
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(AuthError):
            # Passing a non-empty secret_key acts as the token, but account ID is still missing
            await provider.connect("cf_token_value")


# ---------------------------------------------------------------------------
# test_estimate_returns_cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_estimate_returns_cost() -> None:
    """estimate() returns a CostEstimate with provider='cloudflare'."""
    provider = CloudflareProvider()
    config = DeploymentConfig(
        name="test-worker",
        entry_point="src/worker.js",
        runtime=RuntimeType.NODEJS,
        memory_mb=128,
        timeout_ms=10_000,
        replicas=1_000_000,  # simulate 1M requests
    )
    estimate = await provider.estimate(config)

    assert isinstance(estimate, CostEstimate)
    assert estimate.provider == "cloudflare"
    assert estimate.token == "USD"
    assert estimate.amount >= 0
