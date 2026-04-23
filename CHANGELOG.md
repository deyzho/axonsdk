# Changelog

All notable changes to the AxonSDK Python package are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.12] — 2026-04-22

### Fixed
- **Packaging:** `[all]` optional-dependency extra self-referenced the wrong distribution name (`axon[...]` instead of `axonsdk-py[...]`). `pip install "axonsdk-py[all]"` now installs all optional extras correctly. Previously pip would attempt to resolve a separate `axon` package from PyPI.
- **Version drift:** `axon.__version__` is now read dynamically from the installed package metadata via `importlib.metadata` instead of a hard-coded string, so it can no longer lag behind the `pyproject.toml` version. The 0.1.11 wheel on PyPI reported `__version__ = "0.1.6"` — consumers on 0.1.12+ get the correct version.

### Added
- **Typing:** `src/axon/py.typed` (PEP 561 marker) — downstream `mypy` and `pyright` now see the public API's type hints instead of falling back to `Any`.

---

## [0.1.11] — 2026-04-20

### Changed
- **Brand:** AxonSDK consistency pass across CLI `--help` banner, README, and Python landing page. "Axon" (bare) replaced with "AxonSDK" in user-facing copy to disambiguate from the unrelated company of that name.
- **Landing:** Unified AxonSDK landing at `axonsdk.dev`; Python-specific landing moved to `py.axonsdk.dev`.

### Fixed
- **Stale brand references:** `@phonixsdk/*` package names replaced with `@axonsdk/*` throughout README cross-references.
- **OpenAI-compatible endpoint:** removed stale `phonixsdk` `owned_by` value from `/v1/models` response payload.
- **README install-vs-import:** clarified that the PyPI distribution is `axonsdk-py` while the import name remains `axon` (same pattern as `beautifulsoup4` → `from bs4 import`).

---

## [0.1.10] — 2026-04-19

This is the first version actually published to PyPI. Versions 0.1.0 through 0.1.9 existed as git tags during pre-PyPI development but were never published to the registry.

### Added
- **PyPI Trusted Publishing:** OIDC-backed release pipeline — no long-lived tokens.
- **Providers:** Implementation of `COST` routing strategy across all cloud backends.
- **Security:** 30-day post-close remediation — SSRF protection hardening, LRU rate-limit store, CI hardening (ruff + mypy strict + pytest-cov).
- **Inference:** Test suite added for the OpenAI-compatible handler; auth and parameter forwarding fixed.

### Changed
- **PyPI distribution name:** finalised as `axonsdk-py`. `axon`, `axonpy`, `axon-sdk`, and `axonsdk` all failed PyPI's similarity check against existing packages.
- **README:** CI badge, correct PyPI install name, ecosystem section linking the companion TS monorepo.
- **SECURITY.md:** updated contact and disclosure timeline.

---

## [0.1.7] — 2026-04-14

### Changed
- **Domain:** All documentation, metadata, and contact addresses updated from `axon.dev` to `axonsdk.dev`
- **pyproject.toml:** `Homepage` updated to `https://axonsdk.dev`; `Documentation` URL added (`https://axonsdk.dev/docs`)
- **README:** Added `axonsdk.dev` badge links at top; footer updated with new domain and contact
- **Tests:** `conftest.py` fixture endpoint updated to `edge.axonsdk.dev`

---

## [0.1.6] — 2026-04-13

### Added
- **Security:** Added `169.254.x` (AWS EC2 IMDS, Azure IMDS, GCP metadata) to SSRF blocklist in `security.py`
- **Security:** Added IPv6 link-local (`fe80::`) to SSRF blocklist
- **Security:** Added DNS rebinding defence — resolves hostname to IP and re-validates against private-range blocklist
- **Security:** `_looks_like_ip()` helper to skip DNS resolution for raw IP literals
- **Licensing:** Full Apache-2.0 `LICENSE` file with explicit patent grant
- **Security policy:** `SECURITY.md` with responsible disclosure policy (48h ack, 90-day deadline)
- **CI:** `ruff check` lint step in GitHub Actions
- **CI:** `pytest --cov` with Codecov upload (Python 3.11 matrix leg)
- **CI:** SBOM generation via `anchore/sbom-action` (SPDX format)
- **CI:** Python 3.13 added to test matrix (now 3.11, 3.12, 3.13)
- **Tests:** Provider tests for all 10 providers
- `CONTRIBUTING.md` — contributor guide with provider implementation template
- `ROADMAP.md` — v0.1–v1.0 milestones

### Changed
- **Dependencies:** `eth-account` moved from core to optional `blockchain` extra — install with `pip install axonsdk-py[blockchain]` (required for Akash, Acurast, Fluence, Koii providers)
- **CI:** `--ignore-missing-imports` flag removed from CI mypy invocation — now configured permanently in `[tool.mypy]`
- **CI:** `mypy` now reads `ignore_missing_imports = true` from `pyproject.toml`
- **Project URLs:** Fixed `project.urls` in `pyproject.toml` — `Repository` now correctly points to `deyzho/axon`

### Fixed
- `pyproject.toml` project URLs corrected from `deyzho/axonsdk` to `deyzho/axon`

---

## [0.1.5] — 2026-04-12

### Added
- AWS Lambda provider (`axon[aws]`) — boto3 + STS credential validation
- GCP Cloud Run provider (`axon[gcp]`) — Application Default Credentials via google-auth
- Azure Container Instances provider (`axon[azure]`) — OAuth2 client credentials flow
- Cloudflare Workers provider — no extra dependencies
- Fly.io Machines provider — no extra dependencies
- `teardown(deployment_id)` method on all 10 providers and `AxonClient`
- `axon teardown <id>` CLI command
- Exponential backoff retry utility (`axon.utils.retry.with_retry`)

---

## [0.1.2] — 2026-04-12

### Changed
- De-emphasised DePIN branding in public-facing assets
- Updated package metadata and version numbers

---

## [0.1.0] — 2026-04-11

### Added
- Initial release of the Axon Python SDK
- `AxonClient` — single-provider async facade
- `AxonRouter` — multi-provider routing with circuit breaker + health monitor
- Five decentralised compute providers: io.net, Akash, Acurast, Fluence, Koii
- OpenAI-compatible inference endpoint (`axon[inference]`) — FastAPI + uvicorn
- CLI (`axon init`, `axon auth`, `axon deploy`, `axon status`, `axon send`)
- Pydantic v2 models with field constraints (`DeploymentConfig`, `Deployment`, `CostEstimate`)
- SSRF prevention and secret filtering in all providers
- mypy strict mode + ruff linting
- pytest test suite for all providers
- CI/CD via GitHub Actions (test + PyPI publish)
- Apache-2.0 licence

---

[0.1.7]: https://github.com/deyzho/axon/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/deyzho/axon/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/deyzho/axon/compare/v0.1.2...v0.1.5
[0.1.2]: https://github.com/deyzho/axon/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/deyzho/axon/releases/tag/v0.1.0
