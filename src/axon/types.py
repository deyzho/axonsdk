"""Pydantic models and type aliases for the Axon SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


ProviderName = Literal[
    # Edge / decentralised
    "ionet", "akash", "acurast", "fluence", "koii",
    # Cloud
    "aws", "gcp", "azure", "cloudflare", "fly",
]


class RuntimeType(str, Enum):
    NODEJS = "nodejs"
    WASM = "wasm"
    DOCKER = "docker"


class RoutingStrategy(str, Enum):
    LATENCY = "latency"        # Route to fastest provider
    COST = "cost"              # Route to cheapest provider
    ROUND_ROBIN = "round_robin"
    FAILOVER = "failover"      # Primary + fallbacks


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class DeploymentConfig(BaseModel):
    """Configuration for a deployment."""

    name: str = Field(
        ...,
        description="Human-readable deployment name",
        pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$',
    )
    entry_point: str = Field(..., description="Path to entry point file")
    runtime: RuntimeType = RuntimeType.NODEJS
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    memory_mb: int = Field(default=512, ge=128, le=65536)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    replicas: int = Field(default=1, ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Deployment(BaseModel):
    """A deployed processor on a provider."""

    id: str
    name: str
    provider: ProviderName
    status: Literal["pending", "active", "stopped", "failed"]
    created_at: datetime
    endpoint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CostEstimate(BaseModel):
    """Cost estimate for a deployment."""

    provider: ProviderName
    token: str = Field(..., description="Native token symbol (e.g. IO, AKT, ACU)")
    amount: float
    usd_estimate: float | None = None
    per_hour: bool = True
    breakdown: dict[str, float] = Field(default_factory=dict)


class Message(BaseModel):
    """A message sent to or received from a processor."""

    id: str | None = None
    processor_id: str
    payload: Any
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderHealth(BaseModel):
    """Health metrics for a provider."""

    provider: ProviderName
    status: HealthStatus
    latency_ms: float | None = None
    success_rate: float | None = None  # 0.0 - 1.0
    last_checked: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None
