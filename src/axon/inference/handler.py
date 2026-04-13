"""AxonInferenceHandler — OpenAI-compatible FastAPI inference endpoint."""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as exc:
    raise ImportError(
        "FastAPI is required for the inference handler. "
        "Install it with: pip install axon[inference]"
    ) from exc

from axon.inference.router import AXON_MODELS, AxonInferenceRouter


def create_inference_app(secret_key: str, **kwargs: Any) -> FastAPI:
    """
    Create a FastAPI app exposing an OpenAI-compatible inference API.

    Usage:
        import uvicorn
        from axon.inference import AxonInferenceHandler

        app = AxonInferenceHandler(secret_key="...").app
        uvicorn.run(app, host="0.0.0.0", port=8000)
    """
    app = FastAPI(title="Axon Inference API", version="0.1.0")
    router = AxonInferenceRouter({"secret_key": secret_key, **kwargs})

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        models = [
            {
                "id": model_id,
                "object": "model",
                "owned_by": "axonsdk",
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

        try:
            if stream:
                # route() returns an AsyncGenerator when stream=True —
                # wrap it in a StreamingResponse with SSE framing.
                async_gen: AsyncGenerator[Any, None] = router.route(
                    model=model, messages=messages, stream=True
                )  # type: ignore[assignment]

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
                result = await router.route(model=model, messages=messages, stream=False)
                return JSONResponse(result)

        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await router.close()

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
