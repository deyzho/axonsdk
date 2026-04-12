# Axon

> Provider-agnostic edge compute SDK for AI workload routing — Python edition.

Axon routes your AI workloads to the fastest, cheapest available edge compute provider — io.net, Akash, Acurast, Fluence, or Koii — through a single unified interface.

## Install

```bash
pip install axon
# With OpenAI-compatible inference server:
pip install axon[inference]
```

## Quickstart

```python
from axon import AxonClient, DeploymentConfig

async with AxonClient(provider="ionet", secret_key="...") as client:
    estimate = await client.estimate(config)
    deployment = await client.deploy(config)
    await client.send(deployment.id, {"prompt": "Hello"})
```

## Multi-provider routing

```python
from axon import AxonRouter, RoutingStrategy

async with AxonRouter(
    providers=["ionet", "akash", "acurast"],
    secret_key="...",
    strategy=RoutingStrategy.LATENCY,
) as router:
    deployment = await router.deploy(config)
```

## OpenAI-compatible inference

```python
import uvicorn
from axon.inference import AxonInferenceHandler

handler = AxonInferenceHandler(secret_key="...")
uvicorn.run(handler.app, host="0.0.0.0", port=8000)
```

Then hit `POST /v1/chat/completions` with any OpenAI-compatible client.

**Available models:**
- `axon-llama-3-70b` — io.net A100
- `axon-mistral-7b` — io.net RTX4090
- `axon-llama-3-8b` — Akash container
- `axon-tee-phi-3-mini` — Acurast TEE

## CLI

```bash
axon init
axon auth ionet
axon deploy
axon status
axon send <processor-id> '{"prompt": "hello"}'
```

## License

Apache-2.0 © [Axon](https://axon.dev)
