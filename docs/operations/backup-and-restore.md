# Backup and restore

## What must be backed up

| Store              | Backed up                    | Why                                                                                                                           |
| ------------------ | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **PostgreSQL**     | **Yes, always**              | Organisations, users, API key hashes, registries, price books, audit log. Losing it makes every stored trace uninterpretable. |
| **ClickHouse**     | Depends                      | Replayable from the bus within its retention window. Beyond that, gone.                                                       |
| **Object storage** | Versioning off, lifecycle on | Payloads are content-addressed and expendable by policy                                                                       |
| **Redis**          | No                           | Rate limit counters and idempotency records. Loss degrades to permissive and at-least-once.                                   |

PostgreSQL is small — gigabytes — and irreplaceable. ClickHouse is large and
partially reconstructible. Back up accordingly.

## PostgreSQL

The Terraform module configures 14-day automated backups with a final snapshot
on delete and deletion protection, for `environment = "production"`.

Restore:

```console
$ aws rds restore-db-instance-to-point-in-time \
    --source-db-instance-identifier acme-aiobs-metadata \
    --target-db-instance-identifier acme-aiobs-metadata-restored \
    --restore-time 2026-08-01T09:00:00Z
```

Then point `AIOBS_DATABASE__URL` at the restored instance and restart. Run
`python -m alembic upgrade head` — a restore to a point before the last
migration leaves the schema behind.

**Test the restore.** A backup that has never been restored is a hypothesis.

## ClickHouse

Three options, in increasing order of cost:

**Replay from the bus.** Within the bus retention window, reset the consumer
group and let the worker rebuild. Ingest is idempotent, so replay produces no
duplicates. This is the cheapest recovery and it is why the bus retention window
is a capacity decision, not an afterthought.

**Backup to object storage.** ClickHouse's native `BACKUP` command, or your
managed provider's snapshots. Per partition, so a daily backup of yesterday's
partition is cheap.

**Accept the loss.** For many deployments, losing a day of trace history is an
inconvenience rather than an incident, and paying to prevent it is not obviously
correct. Decide deliberately rather than by default.

## Object storage

Versioning is **off** deliberately: payloads are content-addressed and immutable,
so versioning stores a second copy of data that retention exists to delete.

The lifecycle rules are the safety net in the other direction — they bound how
long a payload can exist even if the platform's own sweep is unhealthy.

## Recovery objectives

Realistic targets for the reference deployment:

|                | Target                 | Bounded by           |
| -------------- | ---------------------- | -------------------- |
| PostgreSQL RPO | 5 minutes              | continuous backup    |
| PostgreSQL RTO | 30 minutes             | restore and restart  |
| ClickHouse RPO | 0 within bus retention | bus retention window |
| ClickHouse RTO | hours                  | replay throughput    |

## Disaster recovery drill

Worth running once a quarter:

1. Restore PostgreSQL to a scratch instance.
2. Point a scratch API at it and the real ClickHouse.
3. Sign in. Open a trace. Check the registries resolve.
4. Note what was missing and how long it took.

The step that usually fails is the one nobody thought about: the JWT secret in
the scratch environment differs, so nobody can sign in. That is exactly the kind
of thing a drill exists to find.

## See also

- [Capacity planning](capacity.md)
- [Secrets and rotation](secrets.md)
