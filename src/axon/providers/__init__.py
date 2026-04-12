"""Axon provider implementations."""

from axon.providers.base import IAxonProvider
from axon.providers.ionet import IoNetProvider
from axon.providers.akash import AkashProvider
from axon.providers.acurast import AcurastProvider
from axon.providers.fluence import FluenceProvider
from axon.providers.koii import KoiiProvider
from axon.types import ProviderName

PROVIDER_REGISTRY: dict[ProviderName, type[IAxonProvider]] = {
    "ionet": IoNetProvider,
    "akash": AkashProvider,
    "acurast": AcurastProvider,
    "fluence": FluenceProvider,
    "koii": KoiiProvider,
}


def get_provider(name: ProviderName) -> IAxonProvider:
    """Instantiate a provider by name."""
    cls = PROVIDER_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name!r}. Choose from {list(PROVIDER_REGISTRY)}")
    return cls()
