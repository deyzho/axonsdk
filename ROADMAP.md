# AxonSDK (Python) — Roadmap

> **Current release:** `axonsdk-py` v0.1.12 — Feature-complete for 10 providers, multi-provider routing, OpenAI-compatible inference, and CLI.

Priorities may shift based on community feedback and provider availability. This roadmap reflects the Python SDK ([`deyzho/axon`](https://github.com/deyzho/axon)). The TypeScript monorepo ([`deyzho/axon-ts`](https://github.com/deyzho/axon-ts)) has its own companion roadmap.

Dates are targets, not commitments. Each release ships when its acceptance criteria are met.

---

## v0.1.x — Shipped (2026-04)

All 10 compute providers implemented; async client + multi-provider router; CLI complete; supply-chain and security hardening landed.

### Providers
- io.net — GPU clusters (A100, H100, RTX), job submission, HTTP messaging
- Akash Network — container deployments via SDL, HTTP lease messaging
- Acurast — TEE messaging and deployment
- Fluence — serverless P2P messaging
- Koii — distributed task-node messaging
- AWS — Lambda, ECS/Fargate via boto3 with STS credential validation
- Google Cloud — Cloud Run, Cloud Functions via Application Default Credentials
- Azure — Container Instances, Functions via OAuth2 client credentials
- Cloudflare Workers — no extra dependencies (httpx is core)
- Fly.io Machines — no extra dependencies

### SDK + CLI
- `AxonClient` — single-provider async facade
- `AxonRouter` — multi-provider routing with circuit breaker and health monitor
- `axon[inference]` — FastAPI-backed OpenAI-compatible inference endpoint
- CLI: `axon init`, `axon auth`, `axon deploy`, `axon status`, `axon send`, `axon teardown`
- Exponential backoff retry utility (`axon.utils.retry.with_retry`)

### Quality and security
- SSRF prevention + DNS rebinding defence in `security.py` (RFC-1918, loopback, IPv6 link-local, AWS/Azure/GCP IMDS endpoints all blocked)
- mypy strict across the codebase
- ruff lint gate in CI
- Python 3.11 / 3.12 / 3.13 CI matrix
- Apache-2.0 license with explicit patent grant
- SECURITY.md with responsible disclosure (48h ack, 90-day deadline)
- SBOM generation per build (Anchore SPDX)
- PyPI publish via OIDC Trusted Publishing (no long-lived tokens)
- PEP 561 `py.typed` marker — downstream mypy/pyright pick up typed imports

---

## v0.2 — Operator UX (target: 2026-Q3)

**Theme: deployed workloads are only useful if operators can see and steer them.**

- [ ] `axon logs <deployment_id>` — tail deployment stdout and runtime events
- [ ] `axon update <deployment_id>` — redeploy with new code, preserving routing config
- [ ] `axon stop <deployment_id>` — graceful shutdown without full teardown
- [ ] Structured logging — JSON output with configurable level, ready for log aggregators
- [ ] Persistent leases on Akash / io.net — no cold-start latency for long-running jobs
- [ ] Coverage threshold gate — enforce ≥85% for core modules in CI
- [ ] Per-provider lazy imports — refine `eth-account` import guard so core install stays slim

### Acceptance criteria for v0.2
- All three new CLI commands have integration tests against at least one real provider sandbox
- Coverage gate added to `.github/workflows/publish.yml`
- Core `pip install axonsdk-py` (no extras) completes in under 20s on a cold cache

---

## v0.3 — Provider trust (target: 2026-Q4)

**Theme: enterprise adoption requires proof the integrations actually work.**

- [ ] Live provider integration tests in CI — run against sandboxes weekly, gated at release (not every PR for cost reasons)
- [ ] Provider health dashboard at `status.axonsdk.dev` — real latency + error rates, populated from production synthetic workloads
- [ ] `axon benchmark` — run latency and cost benchmarks across active providers
- [ ] Template marketplace — browse and install community templates via `axon template install <name>`
- [ ] LangChain integration — `AxonLLM` class compatible with LangChain chat models

### Acceptance criteria for v0.3
- At least one real production workload running against each of the 10 providers for ≥30 days
- Status dashboard publishes uptime and latency history, not just current state
- Template registry has at least five community-contributed templates

---

## v0.4 — LLM routing (target: 2027-Q1)

**Theme: unify the LLM client layer so the SDK answers "where should this request run" at the model level, not just the compute level.**

- [ ] `AxonLLMClient` — unified LLM client routing across Claude, Gemini, GPT-4, and self-hosted models
- [ ] Multi-provider simultaneous deploy — deploy to 3 providers, route traffic to fastest
- [ ] Token-based cost tracking per request
- [ ] Model capability registry — which models are available on which providers
- [ ] Streaming response aggregation across providers (SSE-first, merging headers sensibly)
- [ ] Ollama local provider — route to local GPU if available, cloud otherwise

---

## v1.0 — Production-ready (target: 2027-Q2)

**Theme: make the 1.0 promise credible. No breaking changes after this without a major bump.**

### Hard requirements before v1.0 is cut
1. **All 10 providers have green integration tests** running in CI against provider sandboxes at least weekly
2. **At least one named reference customer** per cloud (AWS / GCP / Azure / Cloudflare / Fly.io), willing to be quoted
3. **Deprecation policy in effect**: any API removed between v1.0 and v2.0 must have been marked with `DeprecationWarning` for at least one minor version
4. **Documentation site live** with full API reference, guides, and a working migration page from `0.x` → `1.0`
5. **Semver-strict commitment** documented in the README and enforced by CI via a public-API stability check
6. **Full type coverage** — zero `Any` leaks in the public API of `axon` module (verified via a dedicated mypy run without `ignore_missing_imports` on public interfaces)
7. **Security audit** — third-party review of the client, router, inference handler, and provider adapters

### The v1.0 release itself
- [ ] Drop all `0.x.y` deprecated surfaces in a single breaking release
- [ ] Publish a migration guide covering every renamed or removed API from the `0.x` line
- [ ] Tag `v1.0.0rc1` at least 4 weeks before `v1.0.0` to give downstream time to test
- [ ] `AxonClient` connection pooling + HTTP keep-alive tuned for production workloads
- [ ] Advanced circuit breaker config (per-provider failure thresholds and recovery timeouts)
- [ ] SLA monitoring and alerting hooks

---

## Long-term (post-1.0, no timeline)

- **Streaming results** — push-based result delivery without polling
- **Cost analytics** — per-request cost breakdown and optimisation recommendations across providers
- **SLA routing** — route based on latency SLA targets, not just current metrics
- **VSCode extension** — provider health, deployment status, one-click deploy from the editor
- **AxonSDK-native observability** — OpenTelemetry instrumentation across the SDK with per-provider spans
- **Dashboard web UI** — deployment list, live health, cost breakdown (parity with CLI `axon status`)

---

## Not on the roadmap

- **Proprietary cloud** — AxonSDK will remain Apache-2.0 open source
- **Vendor lock-in** — provider switching will always be a one-line config change
- **Monolithic install** — provider integrations will always be independently installable extras

---

## Versioning policy

AxonSDK follows [Semantic Versioning 2.0](https://semver.org/spec/v2.0.0.html). See [`CONTRIBUTING.md`](./CONTRIBUTING.md#versioning-policy) for the full breaking-change policy.

Short version:
- **`0.x.y`** (current) — minor bumps may contain breaking changes, each documented with a `Changed — Breaking` + `Migration` section in `CHANGELOG.md`.
- **`≥1.0`** (target 2027-Q2) — breaking changes require a major bump; deprecations are announced one minor version before removal.

The public API is the exported surface of `axon` (as defined by `axon.__all__`), the `axon` CLI commands, the FastAPI handler exported by `axon.inference`, and the provider base class at `axon.providers.base.IAxonProvider`. Breaking changes to any of these will be called out in the CHANGELOG and Release notes.

---

*Roadmap items may shift based on community feedback and provider API availability. Open a GitHub Discussion to suggest features or vote on priorities.*
