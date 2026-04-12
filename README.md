# Axon

> Provider-agnostic edge compute SDK for AI workload routing — Python edition.

Axon routes your AI workloads to the fastest, cheapest available compute provider — io.net, Akash, Acurast, Fluence, Koii, AWS, GCP, Azure, Cloudflare, or Fly.io — through a single unified interface.

> **Pre-release:** v0.1.x is early access. APIs may change between minor versions.

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

## Cloud providers

Install optional extras for AWS, GCP, or Azure:

```bash
pip install axon[aws]          # boto3
pip install axon[gcp]          # google-auth
pip install axon[azure]        # azure-identity + azure-mgmt-containerinstance
pip install axon[cloudflare]   # Cloudflare Workers (no extra deps)
pip install axon[fly]          # Fly.io (no extra deps)
pip install axon[all]          # everything
```

Then authenticate:

```bash
axon auth aws
axon auth gcp
axon auth azure
axon auth cloudflare
axon auth fly
```

## CLI

```bash
axon init
axon auth ionet
axon deploy
axon status
axon send <processor-id> '{"prompt": "hello"}'
```

## License

Apache-2.0 © [Axon](https://github.com/deyzho/axon)
