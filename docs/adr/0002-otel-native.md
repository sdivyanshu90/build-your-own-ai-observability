# ADR-0002: Be OpenTelemetry-native, not OpenTelemetry-inspired

## Status

Accepted.

## Context

AI observability tools tend to invent their own tracing model — "generations",
"observations", "runs" — mapped onto OpenTelemetry at the edges if at all. That
gives a vocabulary tuned to the domain at the cost of interoperating with
nothing.

Most teams adopting an AI observability platform already run OpenTelemetry.

## Decision

Use OpenTelemetry concepts wherever one exists. Trace ids are W3C trace ids.
Span kinds are OTel span kinds. Context propagation is `traceparent`. Ingest
accepts OTLP over HTTP, protobuf and JSON.

Add `aiobs.*` attributes only for what OTel does not yet describe, and prefer
the OTel GenAI conventions over inventing a name.

## Consequences

**Good.** An existing OTel Collector can export here with a configuration
change. Existing instrumentation for HTTP, databases and queues produces spans
this platform understands. A trace can span AI and non-AI services because they
share a trace id. Nobody has to learn a second tracing model.

**Costs.** The GenAI conventions are still evolving, so some `gen_ai.*`
attributes will change and need migrating. OTel's model has no notion of a
"retrieval step", so category is an `aiobs.*` addition and applications must set
it. Some of what the platform wants — rank movement, context composition —
does not fit an attribute cleanly and becomes a derived table.

## Alternatives considered

**A proprietary model with an OTLP adapter.** Cleaner domain vocabulary,
and an adapter that loses information in both directions. Every integration
becomes bespoke.

**OTel with no extensions.** Everything AI-specific ends up in unstructured
attributes, which cannot be queried efficiently and cannot be validated.

**Wait for the GenAI conventions to stabilise.** They have been evolving for two
years. Building on them and migrating as they change costs less than not
building.
