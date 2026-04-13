"""Tests for the AWSProvider."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from axon.exceptions import AuthError, DeploymentError, ProviderError
from axon.providers.aws import AWSProvider
from axon.types import CostEstimate, DeploymentConfig, RuntimeType


# ---------------------------------------------------------------------------
# test_connect_requires_credentials
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_requires_credentials() -> None:
    """connect() raises AuthError when boto3.Session.client('sts').get_caller_identity fails."""
    provider = AWSProvider()

    mock_sts = MagicMock()
    mock_sts.get_caller_identity.side_effect = Exception("No credentials found")

    mock_session = MagicMock()
    mock_session.client.return_value = mock_sts

    mock_boto3 = MagicMock()
    mock_boto3.Session.return_value = mock_session

    with patch.dict("os.environ", {}, clear=True):
        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            with pytest.raises(AuthError, match="AWS authentication failed"):
                await provider.connect("")


# ---------------------------------------------------------------------------
# test_deploy_requires_role_arn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deploy_requires_role_arn() -> None:
    """deploy() raises DeploymentError when AWS_LAMBDA_ROLE_ARN is not set."""
    provider = AWSProvider()
    provider._connected = True
    provider._boto_session = MagicMock()  # Pretend we're connected

    config = DeploymentConfig(
        name="test-fn",
        entry_point="src/handler.py",
        runtime=RuntimeType.NODEJS,
        memory_mb=512,
        timeout_ms=30_000,
    )

    # AWS_LAMBDA_ROLE_ARN absent — should trigger DeploymentError before touching the FS
    with patch.dict("os.environ", {}, clear=True):
        # Patch Path.exists to return True so the entry_point check passes
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_bytes", return_value=b"print('hello')"):
                with patch("pathlib.Path.read_text", return_value="print('hello')"):
                    with pytest.raises(DeploymentError, match="AWS_LAMBDA_ROLE_ARN"):
                        await provider.deploy(config)


# ---------------------------------------------------------------------------
# test_estimate_returns_cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_estimate_returns_cost() -> None:
    """estimate() returns a CostEstimate with provider='aws'."""
    provider = AWSProvider()
    config = DeploymentConfig(
        name="test-fn",
        entry_point="src/handler.py",
        runtime=RuntimeType.NODEJS,
        memory_mb=512,
        timeout_ms=30_000,
    )
    estimate = await provider.estimate(config)

    assert isinstance(estimate, CostEstimate)
    assert estimate.provider == "aws"
    assert estimate.token == "USD"
    assert estimate.amount >= 0
