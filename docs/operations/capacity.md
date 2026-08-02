# Capacity planning

## Measure, do not extrapolate from this page

The numbers that matter depend on your span size, attribute cardinality, payload
policy and hardware. This page gives you the shape of the problem and the tools
to measure it. It does **not** give you benchmark results, because a benchmark
run on other hardware is not a prediction about yours.

```console
$ python tests/performance/run_load_test.py --rate 500 --duration 60 \
    --endpoint https://your-deployment --api-key aiobs_...
```

The harness reports **achieved** rate, not requested, and separates client-side
backpressure from server latency — so a slow generator is not misreported as a
slow platform.

## What drives volume

**Spans per request.** A simple completion is 2–3 spans. A RAG pipeline is 5–8.
An agent is 10–40. Multiply by request rate before anything else.

**Payload storage.** Storing prompts and completions typically multiplies bytes
by 10–50×, and it is the single largest lever. Production environments default
to storing none.

**Attribute cardinality.** ClickHouse compresses a `LowCardinality` column to
almost nothing and a high-cardinality string to almost nothing less. Putting a
request id in an attribute is fine; putting it in a _grouped_ attribute is what
turns a cheap query into a scan.

**Retrieval documents.** One row per document per retrieval. A pipeline
fetching 20 documents per request produces 20× the request rate in rows — often
more rows than spans.

## Sizing the parts

**ClickHouse** is the one that grows. Order-of-magnitude starting point: spans
compress well (an order of magnitude is typical), retrieval documents less so.
Daily partitions make retention a `DROP PARTITION`, so disk is bounded by
retention rather than by total ingest.

**PostgreSQL** stays small — organisations, users, keys, registries, audit.
Measured in gigabytes, not terabytes. Size it for connection count and auth
latency, not storage.

**The bus** is a buffer, not a store. Size it for how long you want ingest to
survive an analytics outage: `spans/sec × bytes/span × outage seconds`. Six
hours is a reasonable target.

**Redis** holds rate-limit counters and idempotency records with short TTLs.
Small, and loss is survivable.

**Object storage** is `payload bytes × payload retention days`. Lifecycle rules
bound it independently of the platform's own sweep.

## Scaling the workloads

**API** scales with request rate. It does no expensive work: validate,
deduplicate, publish. CPU-bound on JSON parsing and signature verification.

**Worker** scales with bus lag, capped at the partition count. Beyond that,
repartition. The autoscaler uses CPU as a proxy for lag; if you have a lag
metric available, scale on it directly.

## What gets expensive first

In the order it usually happens:

1. **ClickHouse disk**, if retention is long and payloads are stored.
2. **ClickHouse merges**, if partitions are too fine or inserts too small. The
   worker batches for this reason.
3. **Worker CPU**, if payloads are large — redaction and hashing dominate.
4. **PostgreSQL connections**, if the API is scaled wide. Use a pooler.

## Reducing cost

- **Sample.** 10% of successful traces and 100% of errors keeps the failures and
  drops most of the volume.
- **Do not store payloads in production.** The shape of a request is usually
  enough; the text is what costs.
- **Shorten payload retention** before shortening span retention. Payloads are
  the bytes; spans are the answers.
- **Do not put high-cardinality values in grouped attributes.** They are cheap
  to store and expensive to group by.

## See also

- [Sampling and retention](../concepts/sampling-and-retention.md)
- [Runbook](runbook.md)
