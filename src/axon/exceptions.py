"""Axon exception hierarchy."""


class AxonError(Exception):
    """Base exception for all Axon errors."""


class ProviderError(AxonError):
    """Raised when a provider operation fails."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class ConfigError(AxonError):
    """Raised when axon.json is missing or invalid."""


class AuthError(AxonError):
    """Raised when credentials are missing or invalid."""


class DeploymentError(ProviderError):
    """Raised when a deployment operation fails."""


class ConnectionError(ProviderError):
    """Raised when connecting to a provider fails."""
