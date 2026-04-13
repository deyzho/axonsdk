"""Tests for the IoNetProvider."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from axon.exceptions import AuthError, AxonError, ProviderError
from axon.providers.ionet import IoNetProvider
from axon.types import DeploymentConfig, CostEstimate, RuntimeType


# ---------------------------------------------------------------------------
# test_connect_requires_api_key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_requires_api_key() -> None:
    """connect() with an empty key and no IONET_API_KEY env var raises AuthError."""
    provider = IoNetProvider()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(AuthError):
            await provider.connect("")


# ---------------------------------------------------------------------------
# test_send_validates_url
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_validates_url() -> None:
    """send() to a private IP address raises AxonError (via assert_safe_url)."""
    provider = IoNetProvider()
    # Inject a private-IP endpoint directly to bypass the API look-up
    provider._api_key = "test_key"
    provider._endpoints["job_123"] = "https://192.168.1.1/worker"

    # We need a connected client stub so the method doesn't fail before validation
    mock_client = AsyncMock()
    provider._client = mock_client

    with pytest.raises(AxonError, match="private/local"):
        await provider.send("job_123", {"prompt": "hello"})


# ---------------------------------------------------------------------------
# test_send_validates_https
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_validates_https() -> None:
    """send() to an http:// URL raises AxonError (via assert_safe_url)."""
    provider = IoNetProvider()
    provider._api_key = "test_key"
    provider._endpoints["job_456"] = "http://evil.example.com/worker"

    mock_client = AsyncMock()
    provider._client = mock_client

    with pytest.raises(AxonError, match="HTTPS"):
        await provider.send("job_456", {"prompt": "hello"})


# ---------------------------------------------------------------------------
# test_estimate_returns_cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_estimate_returns_cost() -> None:
    """estimate() returns a CostEstimate with provider='ionet'."""
    provider = IoNetProvider()
    config = DeploymentConfig(
        name="test-job",
        entry_point="src/index.py",
        runtime=RuntimeType.NODEJS,
        memory_mb=512,
        timeout_ms=30_000,
    )
    estimate = await provider.estimate(config)

    assert isinstance(estimate, CostEstimate)
    assert estimate.provider == "ionet"
    assert estimate.token == "IO"
    assert estimate.amount > 0
    assert estimate.per_hour is True


# ---------------------------------------------------------------------------
# test_list_deployments_returns_empty_without_credentials
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_deployments_returns_empty_without_credentials() -> None:
    """list_deployments() with no connected client raises ProviderError."""
    provider = IoNetProvider()
    # _client is None — provider is not connected
    with pytest.raises(ProviderError):
        await provider.list_deployments()
