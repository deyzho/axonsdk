"""Shared pytest fixtures for Axon SDK tests."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from axon.types import (
    DeploymentConfig,
    Deployment,
    CostEstimate,
    RuntimeType,
    ProviderName,
)
from datetime import datetime


@pytest.fixture
def sample_deployment_config() -> DeploymentConfig:
    return DeploymentConfig(
        name="test-deployment",
        entry_point="src/index.py",
        runtime=RuntimeType.NODEJS,
        memory_mb=512,
        timeout_ms=30_000,
    )


@pytest.fixture
def sample_deployment() -> Deployment:
    return Deployment(
        id="dep_abc123",
        name="test-deployment",
        provider="ionet",
        status="active",
        created_at=datetime(2026, 1, 1, 12, 0, 0),
        endpoint="https://dep_abc123.edge.axon.dev",
    )
