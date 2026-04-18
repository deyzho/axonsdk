"""AxonInferenceHandler — OpenAI-compatible FastAPI inference endpoint."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as exc:
    raise ImportError(
        "FastAPI is required for the inference handler. "
        "Install it with: pip install axon[inference]"
    ) from exc

from axon.inference.router import AXON_MODELS, AxonInferenceRouter

_RATE_LIMIT_RPM = 60       # requests per minute
_RATE_LIMIT_MAX_KEYS = 10_000  # evict oldest key when store exceeds this size


class _RateLimitStore:
    """Per-handler sliding-window rate limit store with LRU key eviction.

    Tracks request timestamps per API key within a 60-second window.
    When the number of tracked keys exceeds ``_RATE_LIMIT_MAX_KEYS``,
    the oldest-inserted key is evicted to bound memory usage.
    """

    def __init__(self) -> None:
        # OrderedDict preserves insertion order — used for LRU eviction.
        self._store: OrderedDict[str, list[float]] = OrderedDict()

    def is_allowed(self, key: str, now: float) -> bool:
        """Return True if the request is within the rate limit, False if exceeded."""
        window_start = now - 60.0
        timestamps = self._store.get(key, [])
        # Prune expired timestamps
        timestamps = [t for t in timestamps if t > window_start]

        if len(timestamps) >= _RATE_LIMIT_RPM:
            self._store[key] = timestamps
            return False

        timestamps.append(now)
        self._store[key] = timestamps
        self._store.move_to_end(key)  # Mark as recently used

        # Evict oldest key when store grows too large
        if len(self._store) > _RATE_LIMIT_MAX_KEYS:
            self._store.popitem(last=False)

        return True


def create_inference_app(secret_key: str, **kwargs: Any) -> FastAPI:
    """
    Create a FastAPI app exposing an OpenAI-compatible inference API.

    Usage:
        import uvicorn
        from axon.inference import AxonInferenceHandler

        app = AxonInferenceHandler(secret_key="...").app
        uvicorn.run(app, host="0.0.0.0", port=8000)
    """
    router = AxonInferenceRouter({"secret_key": secret_key, **kwargs})
    _rl_store = _RateLimitStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        await router.close()

    app = FastAPI(title="Axon Inference API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def check_auth(request: Request, call_next: Any) -> Any:
        """Require a valid Bearer token on every request."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != secret_key:
            return JSONResponse(
                {
                    "error": {
                        "message": "Invalid API key.",
                        "type": "auth_error",
                        "code": "invalid_api_key",
                    }
                },
                status_code=401,
            )
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next: Any) -> Any:
        key = request.headers.get("Authorization", "anonymous")
        if not _rl_store.is_allowed(key, time.monotonic()):
            return JSONResponse(
                {
                    "error": {
                        "message": "Rate limit exceeded. Max 60 requests per minute.",
                        "code": "rate_limit_exceeded",
                    }
                },
                status_code=429,
            )
        return await call_next(request)

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        models = [
            {
                "id": model_id,
                "object": "model",
                "owned_by": "phonixsdk",
                "description": info["description"],
                "hardware": info["hardware"],
                "provider": info["provider"],
            }
            for model_id, info in AXON_MODELS.items()
        ]
        return JSONResponse({"object": "list", "data": models})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        body = await request.json()
        model = body.get("model")
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        if not messages:
            raise HTTPException(status_code=400, detail="messages is required")

        # Collect extra OpenAI-compatible params (temperature, max_tokens, etc.)
        reserved = {"model", "messages", "stream"}
        extra = {k: v for k, v in body.items() if k not in reserved}

        try:
            if stream:
                # route() is async; awaiting it when stream=True returns an AsyncGenerator.
                async_gen: AsyncGenerator[Any, None] = await router.route(
                    model=model, messages=messages, stream=True, **extra
                )

                async def _sse_generator() -> AsyncGenerator[bytes, None]:
                    try:
                        async for chunk in async_gen:
                            yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
                    except Exception as exc:  # noqa: BLE001
                        yield b"data: " + json.dumps({"error": str(exc)}).encode() + b"\n\n"
                    finally:
                        yield b"data: [DONE]\n\n"

                return StreamingResponse(
                    _sse_generator(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                # route() returns a coroutine when stream=False — await it.
                result = await router.route(model=model, messages=messages, stream=False, **extra)
                return JSONResponse(result)

        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


class AxonInferenceHandler:
    """
    Convenience wrapper around the inference FastAPI app.

    Usage:
        handler = AxonInferenceHandler(secret_key=os.environ["AXON_SECRET_KEY"])
        # handler.app is a FastAPI instance — mount it or run it directly
    """

    def __init__(self, secret_key: str, **kwargs: Any) -> None:
        self.app = create_inference_app(secret_key, **kwargs)
