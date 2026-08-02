# Deployment

## What you need to run

The platform is two stateless deployments — API and worker — plus a web
application. Everything stateful should be a managed service:

| Dependency            | Minimum | Notes                                                            |
| --------------------- | ------- | ---------------------------------------------------------------- |
| PostgreSQL            | 15+     | Small but on the auth hot path. Multi-AZ in production.          |
| ClickHouse            | 24+     | The one that grows. See [capacity](capacity.md).                 |
| Redis                 | 7+      | Rate limits and idempotency. Loss is survivable.                 |
| Kafka or Redpanda     | 3+      | The durable buffer. Replicated — it has no graceful degradation. |
| S3-compatible storage | —       | Payloads. Lifecycle rules as a backstop for the retention sweep. |

The Helm chart deploys none of these deliberately. An observability platform
that loses its own data because a StatefulSet subchart was rescheduled is worse
than no observability platform.

## Helm

```console
$ helm upgrade --install aiobs infrastructure/helm/aiobs \
    --namespace aiobs --create-namespace \
    --set config.publicUrl=https://observability.example.com \
    --set 'config.security.corsAllowOrigins={https://observability.example.com}' \
    --set config.analytics.url=http://clickhouse:8123 \
    --set config.kv.url=redis://redis-master:6379/0 \
    --set config.bus.brokers=kafka-0:9092 \
    --set config.objects.bucket=aiobs-payloads
```

Create the Secret first — the chart never creates one, so `helm get values` is
safe to share:

```console
$ kubectl create secret generic aiobs -n aiobs \
    --from-literal=database-url='postgresql+asyncpg://aiobs:...@host/aiobs' \
    --from-literal=jwt-secret="$(openssl rand -hex 32)" \
    --from-literal=api-key-pepper="$(openssl rand -hex 32)" \
    --from-literal=cursor-secret="$(openssl rand -hex 32)" \
    --from-literal=analytics-password='...' \
    --from-literal=objects-access-key-id='...' \
    --from-literal=objects-secret-access-key='...'
```

Or sync it from a secret manager with the External Secrets Operator, which is
what the Terraform module is set up for.

## Plain Kubernetes

`infrastructure/kubernetes/base/` has the same workloads as kustomize
manifests, for clusters without Helm. `kubectl apply -k`.

## Terraform

`infrastructure/terraform/modules/aiobs/` provisions the managed dependencies
on AWS: the payload bucket with lifecycle rules, RDS, ElastiCache, MSK, an IRSA
role scoped to the bucket's prefixes, and a Secrets Manager entry.

ClickHouse is not included. Running it well means understanding replication,
merges and the Keeper topology; a module that stood up a single node would work
in a demo and lose data in production.

## Rollout order

1. **Migrations first.** Every migration is expand-only, so the old replicas
   keep working against the new schema. The chart runs them as a
   `pre-install,pre-upgrade` hook.
2. **Worker, then API.** The worker tolerates an older message format; the API
   producing a newer one before the worker can read it would fill the DLQ.
3. **Web last.** It is a pure client.

`maxUnavailable: 0` on the API: a dropped span cannot be re-sent by the
application, so capacity never dips during a rollout.

## Health probes

Three, answering three different questions. Conflating them is how a slow
dependency becomes a restart storm.

| Probe     | Endpoint  | Checks dependencies | Purpose                                                                                        |
| --------- | --------- | ------------------- | ---------------------------------------------------------------------------------------------- |
| Startup   | `/health` | no                  | Has the process finished starting?                                                             |
| Liveness  | `/health` | **no**              | Is the process wedged? A ClickHouse outage must not restart the API.                           |
| Readiness | `/ready`  | **yes**             | Should this pod receive traffic? An unhealthy replica leaves the service without being killed. |

The worker has no HTTP surface. Its liveness probe runs
`aiobs-worker --health-check`, which checks a heartbeat file the supervisor
refreshes — so a consumer that is running but no longer consuming is detected,
which a process-alive check would miss.

## Graceful shutdown

On SIGTERM the API stops accepting new requests and finishes in-flight ones
(45 s grace, plus a 5 s `preStop` sleep so the load balancer notices first).

The worker stops _polling_ but finishes and commits the batch in hand (90 s
grace). Killing mid-batch would be safe — handlers are idempotent — but it would
produce duplicate work on every deploy.

## Before production traffic

```console
$ kubectl exec -n aiobs deploy/aiobs-api -- aiobs-admin check-config
$ kubectl exec -n aiobs deploy/aiobs-api -- aiobs-admin check-dependencies
```

`check-config` fails on a development-shaped production configuration: the
in-memory rate limiter, the SQLite analytics driver, a filesystem object store,
anonymous ingest, a wildcard CORS origin, a plaintext credentialed origin, or
insecure cookies.

## See also

- [Configuration reference](configuration.md)
- [Capacity planning](capacity.md)
- [Runbook](runbook.md)
- [Secrets and rotation](secrets.md)
