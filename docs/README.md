# Documentation

## Start here

- [Your first trace](tutorials/first-trace.md) — from nothing to a trace in the UI
- [Architecture overview](architecture/overview.md) — what the parts are and why
- [Data model](concepts/data-model.md) — traces, spans, and what makes an AI span different

## Concepts

| Page                                                         | What it answers                                                         |
| ------------------------------------------------------------ | ----------------------------------------------------------------------- |
| [Data model](concepts/data-model.md)                         | What a trace, span, event and link are here                             |
| [Semantic conventions](concepts/semantic-conventions.md)     | Which attribute names to use, and why the registry drives redaction     |
| [Versioning and lineage](concepts/versioning-and-lineage.md) | How a response is attributed to an exact prompt and model configuration |
| [Cost attribution](concepts/cost-attribution.md)             | How money is computed, and why it is never a float                      |
| [Retrieval](concepts/retrieval.md)                           | What is recorded about a RAG pipeline, and what the diagnostics mean    |
| [Agent trajectories](concepts/agent-trajectories.md)         | How branches, retries, loops and handoffs are modelled                  |
| [Sampling and retention](concepts/sampling-and-retention.md) | What is kept, for how long, and what deletion actually removes          |

## Architecture

| Page                                                     | What it answers                                    |
| -------------------------------------------------------- | -------------------------------------------------- |
| [Overview](architecture/overview.md)                     | Components, boundaries, and the request path       |
| [Ingestion pipeline](architecture/ingestion-pipeline.md) | Validate, enqueue, normalise, cost, store, roll up |
| [Storage](architecture/storage.md)                       | Why three stores, and what lives in each           |
| [Query model](architecture/query-model.md)               | Filters, sorting, keyset pagination, aggregation   |
| [Multi-tenancy](architecture/multi-tenancy.md)           | How isolation is enforced rather than intended     |

## Development

- [Setup](development/setup.md)
- [Workflow](development/workflow.md)
- [Code map](development/code-map.md)

## Operations

- [Deployment](operations/deployment.md)
- [Configuration reference](operations/configuration.md)
- [Runbook](operations/runbook.md)
- [Capacity planning](operations/capacity.md)
- [Secrets and rotation](operations/secrets.md)
- [Backup and restore](operations/backup-and-restore.md)

## Security

- [Threat model](security/threat-model.md)
- [Data handling](security/data-handling.md)
- [Authentication and authorization](security/authentication.md)

## SDKs and ingestion

- [Python SDK](sdk/python.md)
- [TypeScript SDK](sdk/typescript.md)
- [OTLP without an SDK](sdk/otlp.md)

## API

- [Error envelope and codes](api/errors.md)
- [Pagination](api/pagination.md)
- [Filtering and sorting](api/filtering.md)

The full API reference is the OpenAPI schema, served at `/docs` and exportable
with `make openapi`.

## Decisions

- [Architecture decision records](adr/README.md) — the choices that would be
  expensive to reverse, and the alternatives that were rejected

## Testing

- [Strategy](testing/strategy.md)

## Traceability

- [Requirement traceability matrix](traceability.md)
