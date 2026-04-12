"""Tests for AxonClient."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from axon.client import AxonClient
from axon.exceptions import AuthError


@pytest.mark.asyncio
async def test_connect_requires_secret_key() -> None:
    client = AxonClient(provider="ionet", secret_key=None)
    # Clear env var to ensure no fallback
    with patch.dict("os.environ", {}, clear=True):
        client._secret_key = None
        with pytest.raises(AuthError):
            await client.connect()


@pytest.mark.asyncio
async def test_client_context_manager() -> None:
    client = AxonClient(provider="ionet", secret_key="test_key")
    client._provider.connect = AsyncMock()
    client._provider.disconnect = AsyncMock()

    async with client:
        client._provider.connect.assert_called_once_with("test_key")

    client._provider.disconnect.assert_called_once()
