"""Inference-specific router that maps model IDs to providers."""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator

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
    "axon-llama-3-70b-instruct": {
        "provider": "ionet",
        "hardware": "A100",
        "description": "Llama 3 70B Instruct on io.net GPU cluster",
    },
    "axon-qwen-2-72b": {
        "provider": "ionet",
        "hardware": "H100",
        "description": "Qwen 2 72B on io.net H100 cluster",
    },
    "axon-mistral-7b-instruct": {
        "provider": "akash",
        "hardware": "RTX4090",
        "description": "Mistral 7B Instruct on Akash marketplace",
    },
}

# Maps provider name → environment variable holding the inference endpoint URL
_PROVIDER_ENV_VARS: dict[str, str] = {
    "ionet": "IONET_INFERENCE_URL",
    "akash": "AKASH_INFERENCE_URL",
    "acurast": "ACURAST_INFERENCE_URL",
    "fluence": "FLUENCE_INFERENCE_URL",
    "koii": "KOII_INFERENCE_URL",
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
            raise ProviderError(
                "inference",
                f"Unknown model: {model!r}. Available: {list(AXON_MODELS)}",
            )

        if stream:
            return self._route_streaming(model_info, model, messages, **kwargs)
        return await self._route_standard(model_info, model, messages, **kwargs)

    async def _route_standard(
        self,
        model_info: dict[str, Any],
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """POST to the provider's /v1/chat/completions and return parsed JSON."""
        provider = model_info["provider"]
        endpoint_url = self._get_provider_url(provider)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            **kwargs,
        }

        try:
            resp = await self._client.post(
                f"{endpoint_url.rstrip('/')}/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                provider,
                f"Inference request failed (HTTP {exc.response.status_code}): {exc.response.text}",
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                provider,
                f"Could not reach inference endpoint at {endpoint_url}: {exc}",
            ) from exc

        return resp.json()  # type: ignore[no-any-return]

    async def _route_streaming(
        self,
        model_info: dict[str, Any],
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream SSE chunks from the provider's /v1/chat/completions endpoint."""
        provider = model_info["provider"]
        endpoint_url = self._get_provider_url(provider)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            **kwargs,
        }

        try:
            async with self._client.stream(
                "POST",
                f"{endpoint_url.rstrip('/')}/v1/chat/completions",
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    # SSE format: "data: <json>" or "data: [DONE]"
                    if line.startswith("data: "):
                        data = line[len("data: "):]
                        if data == "[DONE]":
                            break
                        try:
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            # Skip malformed chunks
                            continue
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                provider,
                f"Streaming inference request failed (HTTP {exc.response.status_code})",
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                provider,
                f"Could not reach inference endpoint at {endpoint_url}: {exc}",
            ) from exc

    def _get_provider_url(self, provider: str) -> str:
        """Look up and validate the provider's inference endpoint URL from env."""
        env_var = _PROVIDER_ENV_VARS.get(provider)
        if not env_var:
            raise ProviderError(
                "inference",
                f"No known inference endpoint env var for provider: {provider!r}",
            )
        url = os.environ.get(env_var, "")
        if not url:
            raise ProviderError(
                "inference",
                f"Set {env_var} to the inference endpoint for {provider}",
            )
        return url

    async def close(self) -> None:
        await self._client.aclose()
