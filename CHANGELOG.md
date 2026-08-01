# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- End-to-end request tracing over OTLP (HTTP/protobuf and HTTP/JSON) and a
  native batch endpoint, with W3C trace context propagation and span links.
- Prompt, model-configuration and dataset registries with content-addressed
  immutable versions and movable aliases.
- Retrieval visualisation: pipeline stages, per-document scores, rank movement
  through reranking, context composition and unused-document diagnostics.
- Agent trajectory visualisation: branches, retries, loops, handoffs and tool
  calls as a deterministically laid-out DAG.
- Cost and token accounting against effective-dated price books, using exact
  decimal arithmetic; unpriced usage is reported separately rather than as zero.
- Python and TypeScript SDKs with automatic context propagation, two-layer
  redaction, head sampling and a test harness.
- Web application: overview, trace explorer, trace detail with waterfall,
  retrieval and trajectory views, comparison, latency and cost dashboards,
  registries, and administration including an audit-log viewer.
- Analytics conformance suite holding the SQLite and ClickHouse drivers to
  identical behaviour.
- Helm chart, Kubernetes manifests and a Terraform module for the managed
  dependencies.

### Security

- API keys are stored only as keyed hashes and displayed once at creation.
- Startup validation refuses development-shaped configuration in production.
- Credentialed CORS origins must use HTTPS, with no exemption for loopback.

[Unreleased]: https://github.com/aiobs/ai-observability-platform/commits/main
