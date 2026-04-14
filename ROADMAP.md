# Axon SDK (Python) — Roadmap

> **Current release:** v0.1.6 — Feature-complete for 10 providers, multi-provider routing, OpenAI-compatible inference, and CLI.

---

## v0.1.x — Current (Alpha)

**Status:** Active maintenance

- [x] All 10 provider implementations (io.net, Akash, Acurast, Fluence, Koii, AWS, GCP, Azure, Cloudflare, Fly.io)
- [x] `AxonClient` — single-provider facade
- [x] `AxonRouter` — multi-provider routing with circuit breaker + health monitor
- [x] OpenAI-compatible inference endpoint (`axon[inference]`)
- [x] CLI (`axon init`, `axon auth`, `axon deploy`, `axon status`, `axon send`, `axon teardown`)
- [x] GCP Application Default Credentials (google-auth)
- [x] Azure OAuth2 client credentials flow
- [x] AWS boto3 with STS credential validation
- [x] Exponential backoff retry utility
- [x] SSRF prevention + DNS rebinding defence (`security.py`)
- [x] mypy strict mode + ruff linting in CI
- [x] Apache-2.0 licence, SECURITY.md, responsible disclosure

---

## v0.2 — Stability (Q2 2026)

**Theme:** Production readiness + observability

- [ ] `axon logs <deployment_id>` — tail deployment logs
- [ ] `axon update <deployment_id>` — rolling update an active deployment
- [ ] `axon stop <deployment_id>` — graceful shutdown without full teardown
- [ ] Live provider integration tests in CI (sandbox mode for each provider)
- [ ] Full AWS ECS/Fargate support alongside Lambda
- [ ] Cloudflare R2 storage binding support
- [ ] Structured logging (JSON output, configurable log level)
- [ ] SBOM generation in CI (CycloneDX/SPDX)
- [ ] Codecov coverage reporting with threshold enforcement
- [ ] `eth-account` import guard refinement (per-provider lazy import)

---

## v0.3 — LLM Routing (Q3 2026)

**Theme:** First-class LLM inference orchestration

- [ ] `AxonLLMClient` — unified LLM client routing across providers
- [ ] Multi-provider simultaneous deploy (deploy to 3 providers, route traffic to fastest)
- [ ] Persistent leases on Akash / io.net (no cold-start latency)
- [ ] Token-based cost tracking per request
- [ ] `axon benchmark` — run latency/cost benchmarks across active providers
- [ ] Model capability registry (which models are available on which providers)
- [ ] Streaming response aggregation across providers

---

## v0.4 — Developer Experience (Q4 2026)

**Theme:** DX polish and ecosystem integrations

- [ ] Documentation site (Sphinx + ReadTheDocs or Mintlify)
- [ ] VS Code extension — provider health, deployment status in status bar
- [ ] Dashboard web UI — deployment list, live health, cost breakdown
- [ ] Template marketplace — community-contributed deployment templates
- [ ] LangChain integration (`AxonLLM` class for LangChain chat models)
- [ ] Ollama local provider (route to local GPU if available, cloud otherwise)

---

## v1.0 — Stable API (2027)

**Theme:** API stability, enterprise-grade

- [ ] Stable public API with semver guarantees
- [ ] Full API reference documentation (auto-generated)
- [ ] `AxonClient` connection pooling + keep-alive
- [ ] Advanced circuit breaker config (per-provider thresholds)
- [ ] SLA monitoring and alerting hooks
- [ ] Enterprise support tier

---

## Not on the roadmap

- **Proprietary cloud** — Axon will remain Apache-2.0 open source
- **Lock-in** — provider switching will always be a one-line config change
- **Monolithic architecture** — providers will always be independently installable extras

---

*Roadmap items may shift based on community feedback and provider API availability. Open a GitHub Discussion to suggest features or vote on priorities.*
