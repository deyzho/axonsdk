"""Axon provider implementations."""

from axon.providers.base import IAxonProvider
from axon.providers.ionet import IoNetProvider
from axon.providers.akash import AkashProvider
from axon.providers.acurast import AcurastProvider
from axon.providers.fluence import FluenceProvider
from axon.providers.koii import KoiiProvider
from axon.providers.aws import AWSProvider
from axon.providers.gcp import GCPProvider
from axon.providers.azure import AzureProvider
from axon.providers.cloudflare import CloudflareProvider
from axon.providers.fly import FlyProvider
from axon.types import ProviderName

PROVIDER_REGISTRY: dict[ProviderName, type[IAxonProvider]] = {
    # Edge / decentralised
    "ionet": IoNetProvider,
    "akash": AkashProvider,
    "acurast": AcurastProvider,
    "fluence": FluenceProvider,
    "koii": KoiiProvider,
    # Cloud
    "aws": AWSProvider,
    "gcp": GCPProvider,
    "azure": AzureProvider,
    "cloudflare": CloudflareProvider,
    "fly": FlyProvider,
}


def get_provider(name: ProviderName) -> IAxonProvider:
    """Instantiate a provider by name."""
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name!r}. Choose from {list(PROVIDER_REGISTRY)}")
    return cls()
