"""Tests for the AkashProvider."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from axon.exceptions import AuthError, DeploymentError, ProviderError
from axon.providers.akash import AkashProvider
from axon.types import CostEstimate, DeploymentConfig, RuntimeType


# ---------------------------------------------------------------------------
# test_connect_requires_mnemonic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_requires_mnemonic() -> None:
    """connect() with an empty mnemonic and no AKASH_MNEMONIC env var raises AuthError."""
    provider = AkashProvider()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(AuthError):
            await provider.connect("")


@pytest.mark.asyncio
async def test_connect_rejects_invalid_mnemonic_length() -> None:
    """connect() raises AuthError when the mnemonic has the wrong word count."""
    provider = AkashProvider()
    bad_mnemonic = "word " * 8  # 8 words — not 12 or 24
    with patch.dict("os.environ", {"AKASH_MNEMONIC": bad_mnemonic.strip()}, clear=True):
        with pytest.raises(AuthError, match="mnemonic must be 12 or 24 words"):
            await provider.connect(bad_mnemonic)


# ---------------------------------------------------------------------------
# test_deploy_requires_entry_point
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deploy_requires_entry_point() -> None:
    """deploy() raises DeploymentError when the entry_point file does not exist."""
    provider = AkashProvider()
    provider._connected = True

    config = DeploymentConfig(
        name="test-job",
        entry_point="/nonexistent/path/index.js",
        runtime=RuntimeType.NODEJS,
    )
    # Provide a valid AKASH_IPFS_URL so the code gets past that check,
    # then fails at the missing entry point.
    with patch.dict("os.environ", {"AKASH_IPFS_URL": "https://ipfs.example.com"}):
        with pytest.raises(DeploymentError, match="Entry point not found"):
            await provider.deploy(config)


# ---------------------------------------------------------------------------
# test_estimate_returns_cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_estimate_returns_cost() -> None:
    """estimate() returns a CostEstimate with provider='akash' and token='AKT'."""
    provider = AkashProvider()
    config = DeploymentConfig(
        name="test-job",
        entry_point="src/index.js",
        runtime=RuntimeType.NODEJS,
        memory_mb=512,
        timeout_ms=30_000,
    )
    estimate = await provider.estimate(config)

    assert isinstance(estimate, CostEstimate)
    assert estimate.provider == "akash"
    assert estimate.token == "AKT"
    assert estimate.amount > 0
