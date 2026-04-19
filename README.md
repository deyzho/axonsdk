# Axon SDK

[![CI](https://github.com/deyzho/axon/actions/workflows/publish.yml/badge.svg)](https://github.com/deyzho/axon/actions/workflows/publish.yml)
[![PyPI](https://img.shields.io/badge/PyPI-pending-orange)](https://github.com/pypi/support/issues)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](https://pypi.org/project/axonsdk-py/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](./LICENSE)

**[axonsdk.dev](https://axonsdk.dev) · [GitHub](https://github.com/deyzho/axon)**

**One SDK. Any compute. Route AI inference to the fastest, cheapest backend — cloud, edge, or your own infrastructure.**

Axon is a universal AI compute routing layer. Stop rewriting integrations every time you switch providers, hit rate limits, or find a cheaper GPU. Point Axon at any backend — GPU clusters, container clouds, serverless functions, TEE enclaves, or your own servers — and it handles routing, failover, and cost optimisation automatically.

> Axon is to AI compute what httpx is to HTTP — **one client, any backend**.

---

## Supported providers

### Edge & private compute

| Provider | Status | Nodes | Runtime | Cost |
|---|---|---|---|---|
| [io.net](https://io.net) | ✅ Live | GPU clusters (A100, H100, RTX) | nodejs, python | ~$0.40/hr GPU spot |
| [Akash Network](https://akash.network) | ✅ Live | Container compute marketplace | nodejs, docker | Pay-per-use |
| [Acurast](https://acurast.com) | ✅ Live | 237k+ mobile TEE nodes | nodejs, wasm | Pay-per-execution |
| [Fluence](https://fluence.network) | ✅ Live | Serverless function compute | nodejs | Pay-per-ms |
| [Koii](https://koii.network) | ✅ Live | Distributed task nodes | nodejs | Pay-per-task |

### Cloud providers

| Provider | Status | Services | Runtime |
|---|---|---|---|
| [AWS](https://aws.amazon.com) | ✅ Live | Lambda, ECS / Fargate, EC2 | python, nodejs, docker |
| [Google Cloud](https://cloud.google.com) | ✅ Live | Cloud Run, Cloud Functions | python, nodejs, docker |
| [Azure](https://azure.microsoft.com) | ✅ Live | Container Instances, Functions | python, nodejs, docker |
| [Cloudflare Workers](https://workers.cloudflare.com) | ✅ Live | Workers, R2, AI Gateway | nodejs, wasm |
| [Fly.io](https://fly.io) | ✅ Live | Fly Machines | python, nodejs, docker |

> **Provider health dashboard:** Real-time status and latency for all networks → [status.axonsdk.dev](https://status.axonsdk.dev)

---

## Install

```bash
pip install axonsdk-py              # core SDK (mirrors the axon-ts npm packages)
pip install "axonsdk-py[inference]" # + FastAPI OpenAI-compatible server
pip install "axonsdk-py[aws]"       # + boto3
pip install "axonsdk-py[gcp]"       # + google-auth
pip install "axonsdk-py[azure]"     # + azure-identity + azure-mgmt-containerinstance
pip install "axonsdk-py[all]"       # everything
```

> **Note:** Install as `axonsdk-py`, import as `axon`.
>
> ```python
> from axon import AxonClient, AxonRouter
> ```
>
> (The PyPI distribution name differs from the import name — same pattern as `beautifulsoup4` → `from bs4 import`.)

> **Why `axonsdk-py` instead of `axonsdk`?** The `axon`, `axonpy`, and `axon-sdk` names on PyPI are held by unrelated projects, and `axonsdk` is blocked by PyPI's name-similarity rule. `axonsdk-py` mirrors the `axon-ts` repo naming convention (`-py` for Python, `-ts` for TypeScript) while the import path stays `import axon` unchanged.

---

## Quick start

```python
from axon import AxonClient, AxonRouter, RoutingStrategy, DeploymentConfig, RuntimeType

config = DeploymentConfig(
    name="my-inference-job",
    entry_point="src/worker.py",
    runtime=RuntimeType.PYTHON,
    memory_mb=512,
    timeout_ms=30_000,
    replicas=1,
)

# Single provider
async with AxonClient(provider="ionet", secret_key="...") as client:
    estimate = await client.estimate(config)
    print(f"Estimated: {estimate.amount} {estimate.token}/hr")

    deployment = await client.deploy(config)
    await client.send(deployment.id, {"prompt": "Hello"})

# Multi-provider with automatic routing
async with AxonRouter(
    providers=["ionet", "akash", "acurast"],
    secret_key="...",
    strategy=RoutingStrategy.LATENCY,
) as router:
    deployment = await router.deploy(config)
    await router.send({"prompt": "Hello"})
```

---

## CLI

```bash
axon init       # interactive project setup
axon auth ionet # configure credentials
axon deploy     # deploy your workload
axon status     # list active deployments
axon send <id> <msg>    # send a test message
axon teardown <id>      # delete deployment and free resources
```

| Command | Description |
|---|---|
| `axon init` | Interactive setup — generates `axon.json`, `.env`, and template files |
| `axon auth [provider]` | Credential wizard — generates and stores keys for the selected provider |
| `axon deploy` | Bundle and register your deployment |
| `axon run-local` | Run locally with a mock provider runtime |
| `axon status` | List deployments, processor IDs, and live status |
| `axon send <id> <msg>` | Send a test message to a processor node |
| `axon teardown <id>` | Delete a deployment and free provider resources |
| `axon template list` | Show available built-in templates |

Supported providers: `ionet`, `akash`, `acurast`, `fluence`, `koii`, `aws`, `gcp`, `azure`, `cloudflare`, `flyio`

---

## Multi-provider routing

`AxonRouter` routes requests across multiple providers simultaneously, picking the best one on every call based on real-time latency, cost, and availability.

```python
from axon import AxonRouter, RoutingStrategy

async with AxonRouter(
    providers=["ionet", "akash", "acurast", "aws", "gcp"],
    secret_key="...",
    strategy=RoutingStrategy.LATENCY,
    failure_threshold=3,
    recovery_timeout_ms=30_000,
    max_retries=2,
) as router:
    deployment = await router.deploy(config)

    # Automatically picks the highest-scoring provider
    await router.send({"prompt": "Hello"})

    # Health snapshot
    for h in await router.health():
        print(h.provider, h.latency_ms, h.status)
```

### Routing strategies

| Strategy | Best for |
|---|---|
| `LATENCY` | Interactive workloads — always picks the fastest provider |
| `AVAILABILITY` | High uptime — prefers the most reliable provider |
| `COST` | Batch jobs — routes to the cheapest option |
| `BALANCED` | General purpose — equal weight on availability, latency, cost |
| `ROUND_ROBIN` | Even load distribution |

---

## OpenAI-compatible inference endpoint

`axon[inference]` is a drop-in replacement for the OpenAI API that routes requests to the fastest available backend. Switch your existing OpenAI integration in two lines:

```python
import uvicorn
from axon.inference import AxonInferenceHandler

handler = AxonInferenceHandler(
    secret_key="...",
    ionet_endpoint="...",
    akash_endpoint="...",
    strategy="cost",  # 'cost' | 'latency' | 'balanced'
)
uvicorn.run(handler.app, host="0.0.0.0", port=8000)
```

Then point any OpenAI client at `http://localhost:8000`:

```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:8000/v1",
    api_key=AXON_SECRET_KEY,
)

response = client.chat.completions.create(
    model="axon-llama-3-70b",
    messages=[{"role": "user", "content": "Explain edge AI in one paragraph."}],
)
```

### Available models

| Model ID | Backend | Notes |
|---|---|---|
| `axon-llama-3-70b` | io.net | A100 GPU — best quality |
| `axon-mistral-7b`  | io.net | GPU, most cost-efficient |
| `axon-llama-3-8b`  | Akash  | Container compute, moderate cost |
| `axon-tee-phi-3-mini` | Acurast | TEE node — private execution |

---

## Cloud provider authentication

### AWS

```bash
pip install axonsdk-py[aws]
```

Set env vars (or use IAM role / `~/.aws/credentials`):

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

### GCP

```bash
pip install axonsdk-py[gcp]
```

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export GCP_PROJECT_ID=my-project
export GCP_REGION=us-central1
```

### Azure

```bash
pip install axonsdk-py[azure]
```

```bash
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
export AZURE_SUBSCRIPTION_ID=...
export AZURE_RESOURCE_GROUP=my-rg
```

### Cloudflare Workers

```bash
export CF_API_TOKEN=...
export CF_ACCOUNT_ID=...
```

### Fly.io

```bash
export FLY_API_TOKEN=...   # flyctl auth token
export FLY_APP_NAME=...    # flyctl apps create <name>
```

---

## Security

- **Secrets never leave `.env`** — the auth wizard generates keys locally and stores them with `chmod 600`. Never logged or transmitted.
- **SSRF protection** — all HTTP calls validate URLs against a private-IP blocklist and enforce HTTPS.
- **DNS rebinding defence** — resolves hostnames to IPs before opening connections, then re-validates the IP.
- **Prototype pollution prevention** — remote JSON payloads are parsed with key blocklisting; environment maps use `Object.create(null)`.
- **Response size caps** — all provider clients enforce a 1 MiB response cap; mock runtime enforces 4 MiB.
- **Input validation** — `processorId` and deployment names validated for control characters and path traversal sequences.
- **Secret filtering** — environment variables with `_KEY`, `_SECRET`, `_TOKEN`, `_PASSWORD`, `_MNEMONIC`, or `_PRIVATE_KEY` suffixes are stripped before bundle injection.

---

## Project structure

```
axon/
├── src/
│   └── axon/
│       ├── providers/
│       │   ├── ionet.py       # io.net GPU provider
│       │   ├── akash.py       # Akash Network provider
│       │   ├── acurast.py     # Acurast TEE provider
│       │   ├── fluence.py     # Fluence serverless provider
│       │   ├── koii.py        # Koii task node provider
│       │   ├── aws.py         # AWS Lambda / ECS provider
│       │   ├── gcp.py         # Google Cloud Run provider
│       │   ├── azure.py       # Azure Container Instances provider
│       │   ├── cloudflare.py  # Cloudflare Workers provider
│       │   └── fly.py         # Fly.io Machines provider
│       ├── inference/         # OpenAI-compatible FastAPI server
│       ├── cli/               # axon CLI (Typer)
│       └── utils/             # retry, security helpers
└── tests/
    └── providers/             # unit tests for all 10 providers
```

---

## Development

```bash
git clone https://github.com/deyzho/axon.git
cd axon
pip install -e ".[all,dev]"
pytest
mypy src/
```

---

## Contributing

Pull requests are welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) to get started.

High-impact areas:
- Integration tests against live provider sandboxes
- Additional provider support
- Template library

---

## Ecosystem

Axon is the **Python** compute routing SDK. If you're building with **TypeScript / Node.js**, React Native, or deploying via a CLI, see the companion repositories:

| Package | Description |
|---|---|
| [`@axonsdk/sdk`](https://github.com/deyzho/axon-ts) | TypeScript SDK — same providers, same routing strategies |
| [`@axonsdk/mobile`](https://github.com/deyzho/axon-ts) | React Native / Expo SDK for iOS & Android |
| [`@axonsdk/cli`](https://github.com/deyzho/axon-ts) | CLI — `axon init`, `axon deploy`, `axon status` |
| [`@axonsdk/inference`](https://github.com/deyzho/axon-ts) | OpenAI-compatible inference handler for Next.js |

**[axonsdk.dev](https://axonsdk.dev)** — full documentation for the TypeScript ecosystem.

---

## License

Apache-2.0 — see [LICENSE](./LICENSE).

---

**[axonsdk.dev](https://axonsdk.dev)** · deyzho@me.com · Apache-2.0

*Axon is not affiliated with io.net, Akash Network, Acurast, Fluence, or Koii. Provider names and trademarks belong to their respective owners.*
