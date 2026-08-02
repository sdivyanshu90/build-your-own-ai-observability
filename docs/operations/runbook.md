# Runbook

Every response carries a request id. Start there: it is in `X-Request-Id`, in
the error envelope, and in every log line for that request.

---

## Ingest is returning 503

**Meaning:** the bus is unreachable. This is the one dependency with no
graceful degradation — spans cannot be buffered anywhere else.

1. `kubectl exec deploy/aiobs-api -- aiobs-admin check-dependencies`
2. Check broker health and disk. A full broker disk presents as a publish
   timeout, not as a disk alert.
3. SDKs are backing off with jitter and will retry. Spans buffered in
   application processes are lost if those processes restart, so the clock is
   real but short.

**Do not** disable the bus to "get ingest working". Writing straight to
ClickHouse from the request path is how a storage hiccup becomes latency in
every instrumented application.

---

## Worker lag is growing

**Symptom:** traces appear in the UI minutes after the request.

1. Check consumer lag. The autoscaler is tuned on CPU as a proxy; if lag is
   growing while CPU is flat, the worker is blocked on something.
2. Check ClickHouse insert latency and `system.merges`. Merge backlog is the
   usual cause.
3. Check the DLQ: `aiobs-admin dlq list`. A poison batch retries five times
   before landing there, and five slow retries per batch is enough to stall a
   partition.
4. Scale the worker. It is stateless; add replicas up to the partition count.

Beyond the partition count, more replicas do nothing — repartition the topic.

---

## The dead-letter queue is filling

```console
$ aiobs-admin dlq list --limit 20
$ aiobs-admin dlq show <id>        # the batch, the error, the attempt count
$ aiobs-admin dlq replay <id>      # after fixing the cause
```

Most common causes, in order: a schema change the worker does not know about, a
span whose attribute exceeds the size limit, and a ClickHouse column type
mismatch after a partial migration.

A DLQ entry is not data loss — it is data waiting. It becomes loss when nobody
replays it.

---

## Costs look wrong

**Totals lower than expected** — some usage is unpriced. The overview banner
says so and names the models. Add price book entries; existing traces re-price
because the calculation is deterministic and effective-dated.

**Totals higher than expected** — check the cache convention on the price book
entries. If a provider reports input tokens inclusive of cached ones and the
entry says exclusive, cached input is counted twice.

**Totals that do not match the invoice** — compare per model, per day. The
`cost_records` table stores the formula for every priced span, so a discrepancy
resolves to a specific rule rather than to "the platform is wrong".

**Token counts disagree with the provider** — check `usage_source`. `estimated`
means a local tokeniser produced them and a few percent of drift is expected.

---

## A dashboard shows a cliff at the right-hand edge

That is the current bucket, still filling. The API marks it in
`partial_buckets` and the UI draws it dashed with a note.

If it is _not_ marked partial and the drop is real, check worker lag first —
ingestion lag looks exactly like a traffic drop.

---

## Authentication is failing for everyone

1. Was `AIOBS_AUTH__JWT_SECRET` rotated? Rotating it invalidates every issued
   token by design. Users need to sign in again.
2. Is PostgreSQL reachable? The user epoch is checked on every request; if that
   read fails, every request fails.
3. Check clock skew. Tokens carry `iat` and `exp`; more than a minute of skew
   between replicas produces intermittent, confusing failures.

---

## Authentication is failing for one user

Check the audit log for `auth.login.failed` and `auth.locked_out`. Ten failed
attempts triggers a lockout window.

---

## A browser client gets "Failed to fetch" with no status

Almost always CORS. The response never reached JavaScript, so there is no status
to see.

1. Confirm the origin is in `AIOBS_SECURITY__CORS_ALLOW_ORIGINS`. `localhost`
   and `127.0.0.1` are different origins to a browser.
2. Check the response headers directly:
   `curl -i -H 'Origin: https://your-origin' $AIOBS_ENDPOINT/health`.
   No `access-control-allow-origin` means the origin is not allowed.

---

## Retention is not deleting

1. Is the worker running the job runner? `--no-jobs` disables it.
2. Check the sweep's log lines: it deletes in bounded batches and logs counts.
3. Objects are deleted only after the rows that reference them. If rows are
   being deleted but objects are not, the orphan-collection job is failing —
   check object storage credentials.

The S3 lifecycle rule in the Terraform module is the backstop for exactly this
case, so personal data does not outlive its policy because a job was unhealthy.

---

## Restoring after data loss

See [backup-and-restore.md](backup-and-restore.md). In short: PostgreSQL is the
one that must be restored, ClickHouse can be replayed from the bus within its
retention window, and object storage is versioned or it is gone.

---

## Emergency: stop accepting telemetry

```console
$ kubectl scale deploy/aiobs-api --replicas=0 -n aiobs
```

SDKs back off and buffer briefly, then drop. Queries stop too, since it is the
same deployment. Prefer rate limiting a specific key:

```console
$ curl -X POST "$AIOBS_ENDPOINT/v1/api-keys/$KEY_ID/rate-limit" -d '{"per_minute": 0}'
```
