# Contributing to Axon SDK (Python)

Thank you for your interest in contributing! This guide covers everything you need to get started.

## Getting started

### 1. Fork and clone

```bash
git clone https://github.com/<your-username>/axon.git
cd axon
```

### 2. Set up a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows
```

### 3. Install in editable mode with all dev dependencies

```bash
pip install -e ".[all,dev]"
```

### 4. Verify the setup

```bash
pytest tests/ -v
mypy src/axon
ruff check src/axon
```

All three should pass with no errors before you begin.

---

## Project structure

```
axon/
├── src/
│   └── axon/
│       ├── __init__.py           # Public exports
│       ├── client.py             # AxonClient — single-provider facade
│       ├── router.py             # AxonRouter — multi-provider routing + circuit breaker
│       ├── types.py              # Pydantic models (DeploymentConfig, Deployment, etc.)
│       ├── config.py             # Configuration loading
│       ├── exceptions.py         # Exception hierarchy
│       ├── security.py           # SSRF prevention, URL validation, DNS rebinding defence
│       ├── pricing.py            # Cost estimation helpers
│       ├── providers/
│       │   ├── base.py           # IAxonProvider abstract interface
│       │   ├── ionet.py          # io.net GPU provider
│       │   ├── akash.py          # Akash Network provider
│       │   ├── acurast.py        # Acurast TEE provider
│       │   ├── fluence.py        # Fluence serverless provider
│       │   ├── koii.py           # Koii task node provider
│       │   ├── aws.py            # AWS Lambda / ECS provider
│       │   ├── gcp.py            # Google Cloud Run provider
│       │   ├── azure.py          # Azure Container Instances provider
│       │   ├── cloudflare.py     # Cloudflare Workers provider
│       │   └── fly.py            # Fly.io Machines provider
│       ├── inference/
│       │   ├── handler.py        # FastAPI OpenAI-compatible endpoint
│       │   └── router.py         # Inference-specific routing
│       ├── cli/
│       │   └── main.py           # axon CLI (Typer)
│       └── utils/
│           └── retry.py          # Exponential backoff with jitter
└── tests/
    ├── conftest.py               # Shared fixtures
    ├── test_client.py
    ├── test_config.py
    ├── test_router.py
    └── providers/
        └── test_*.py             # One test file per provider
```

---

## Making changes

### Workflow

1. Create a branch: `git checkout -b feat/my-feature`
2. Make your changes
3. Run the full check suite (see below)
4. Commit with a clear message
5. Open a pull request against `master`

### Full check suite

```bash
ruff check src/axon          # Lint
mypy src/axon                 # Type check (strict mode)
pytest tests/ -v --tb=short  # Tests
```

All three must pass before your PR will be reviewed.

---

## Adding a new provider

Each provider is a single Python file in `src/axon/providers/`. Here is a minimal template:

```python
"""MyProvider implementation."""

from __future__ import annotations

from axon.exceptions import AuthError, ProviderError
from axon.providers.base import IAxonProvider
from axon.types import (
    CostEstimate, Deployment, DeploymentConfig,
    HealthStatus, Message, ProviderHealth, ProviderName,
)


class MyProvider(IAxonProvider):

    @property
    def name(self) -> ProviderName:
        return "myprovider"   # Must be added to ProviderName Literal in types.py

    async def connect(self, secret_key: str) -> None:
        # Validate credentials, establish session
        ...

    async def disconnect(self) -> None:
        ...

    async def deploy(self, config: DeploymentConfig) -> Deployment:
        ...

    async def estimate(self, config: DeploymentConfig) -> CostEstimate:
        ...

    async def send(self, deployment_id: str, payload: object) -> None:
        ...

    def on_message(self, handler: ...) -> ...:
        ...

    async def list_deployments(self) -> list[Deployment]:
        ...

    async def health(self) -> ProviderHealth:
        ...

    async def teardown(self, deployment_id: str) -> None:
        ...
```

**Checklist for a new provider:**

- [ ] Add provider to `ProviderName` Literal in `src/axon/types.py`
- [ ] Implement all abstract methods from `IAxonProvider`
- [ ] Add import guard for optional dependencies (`try: import boto3 except ImportError: raise ProviderError(...)`)
- [ ] Add to `src/axon/__init__.py` exports
- [ ] Add to `src/axon/client.py` provider registry
- [ ] Write test file in `tests/providers/test_myprovider.py`
- [ ] Add install extras to `pyproject.toml` if new dependencies are needed
- [ ] Document in `README.md` provider table

---

## Code style

- **Formatter / linter:** ruff (configured in `pyproject.toml`) — `ruff check src/axon` must pass
- **Type checker:** mypy strict mode — `mypy src/axon` must pass with zero errors
- **Line length:** 100 characters
- **Python target:** 3.11+ syntax; use `from __future__ import annotations` in all files
- **No `# type: ignore`** without a specific error code and comment explaining why

---

## Security

Please **do not** open public GitHub issues for security vulnerabilities. Instead email **security@axonsdk.dev**. See [SECURITY.md](./SECURITY.md) for the full disclosure policy.

---

## Questions?

Open a GitHub Discussion or email `hello@axonsdk.dev`.
