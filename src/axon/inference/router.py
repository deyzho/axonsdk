"""Inference-specific router that maps model IDs to providers."""

from __future__ import annotations

from typing import Any

import httpx

from axon.exceptions import ProviderError


AXON_MODELS = {
    "axon-llama-3-70b": {
        "provider": "ionet",
        "hardware": "A100",
        "description": "Llama 3 70B on io.net GPU cluster",
    },
    "axon-mistral-7b": {
        "provider": "ionet",
        "hardware": "RTX4090",
        "description": "Mistral 7B on io.net GPU cluster",
    },
    "axon-llama-3-8b": {
        "provider": "akash",
        "hardware": "container",
        "description": "Llama 3 8B on Akash containerized cloud",
    },
    "axon-tee-phi-3-mini": {
        "provider": "acurast",
        "hardware": "TEE",
        "description": "Phi-3 Mini in Acurast Trusted Execution Environment",
    },
}


class AxonInferenceRouter:
    """Routes OpenAI-compatible inference requests to edge providers."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client = httpx.AsyncClient(timeout=120.0)

    async def route(
        self,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        model_info = AXON_MODELS.get(model)
        if not model_info:
            raise ProviderError("inference", f"Unknown model: {model!r}. Available: {list(AXON_MODELS)}")

        # TODO: route to the appropriate provider based on model_info["provider"]
        raise NotImplementedError(f"Inference routing for {model} not yet implemented")

    async def close(self) -> None:
        await self._client.aclose()
