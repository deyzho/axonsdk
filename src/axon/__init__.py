"""
Axon — Provider-agnostic edge compute SDK for AI workload routing.

Quickstart:
    from axon import AxonClient

    client = AxonClient(provider="ionet", secret_key="your_key")
    await client.connect()
    deployment = await client.deploy(config)
"""

from axon.client import AxonClient
from axon.router import AxonRouter
from axon.exceptions import AxonError, ProviderError, ConfigError
from axon.types import (
    DeploymentConfig,
    Deployment,
    CostEstimate,
    Message,
    ProviderName,
    RoutingStrategy,
    HealthStatus,
)

__version__ = "0.1.0"

__all__ = [
    "AxonClient",
    "AxonRouter",
    "AxonError",
    "ProviderError",
    "ConfigError",
    "DeploymentConfig",
    "Deployment",
    "CostEstimate",
    "Message",
    "ProviderName",
    "RoutingStrategy",
    "HealthStatus",
]
