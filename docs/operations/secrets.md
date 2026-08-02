# Secrets and rotation

## What exists

| Secret                          | Used for                          | Rotating it                                                    |
| ------------------------------- | --------------------------------- | -------------------------------------------------------------- |
| `AIOBS_AUTH__JWT_SECRET`        | Signing access and refresh tokens | Invalidates **every issued token**. Everyone signs in again.   |
| `AIOBS_AUTH__API_KEY_PEPPER`    | Keying the API key hash           | Invalidates **every issued API key**. Every SDK stops sending. |
| `AIOBS_SECURITY__CURSOR_SECRET` | Signing pagination cursors        | Invalidates in-flight cursors. Users re-page. Harmless.        |
| `AIOBS_DATABASE__URL`           | PostgreSQL credentials            | Standard credential rotation                                   |
| `AIOBS_ANALYTICS__PASSWORD`     | ClickHouse                        | Standard                                                       |
| Object storage credentials      | S3                                | Prefer IRSA or an instance role and have none                  |

The first two are the ones with teeth. Neither is on an automatic schedule,
because "every API key stopped working at 3am" is not an acceptable outcome of
a background job.

## Rotating the JWT secret

Users lose their sessions. Do it during a low-traffic window.

```console
$ kubectl create secret generic aiobs -n aiobs \
    --from-literal=jwt-secret="$(openssl rand -hex 32)" \
    --dry-run=client -o yaml | kubectl apply -f -
$ kubectl rollout restart deploy/aiobs-api -n aiobs
```

Ingest is unaffected — API keys do not use the JWT secret.

## Rotating the API key pepper

**This is a breaking change for every integration.** The stored hashes were
computed with the old pepper and cannot be recomputed, because the input is the
secret nobody has.

The procedure is therefore a migration, not a rotation:

1. Announce it. Every application sending telemetry will need a new key.
2. Issue new keys under a second pepper, if you have implemented dual-pepper
   verification, or accept a cutover window.
3. Rotate, restart, and have every integration adopt its new key.

If you are rotating because a pepper leaked, the cutover window is the right
answer and the announcement is after the fact.

## Storing secrets

**Never in the repository.** `.gitignore` excludes `.env` and every `*.key`, the
secret scanner runs in CI on both the working tree and the full history, and
`.env.example` contains only placeholders.

**In Kubernetes**, use the External Secrets Operator against your secret
manager. The Helm chart references keys in an existing Secret and never creates
one, so `helm get values` is safe to share and a rendered manifest is safe to
commit to a GitOps repository.

**In Terraform**, the module writes generated credentials to Secrets Manager and
exports only the ARN. `terraform output` never prints a credential.

## API keys issued by the platform

Format: `aiobs_<env>_<prefix>_<secret>`.

- Stored as HMAC-SHA-256 with the server-side pepper — never reversible.
- The full value is shown **once**, at creation. There is no endpoint that
  returns it, because there is nothing to return.
- The `prefix` is stored in clear and displayed, so a key can be identified in a
  list without being recoverable.
- `last_used_at` is recorded, so unused keys can be found and revoked.

Revocation is immediate: the key row is marked revoked and the auth cache entry
is invalidated.

## If a secret leaks

1. **Rotate first, investigate second.**
2. Check the audit log for what the credential did:
   `GET /v1/audit-events?actor_type=api_key&resource_id=<key id>`.
3. For a leaked API key: revoke it. Ingest from it stops within the cache TTL.
4. For a leaked JWT secret: rotate and restart. Every token is dead immediately.
5. For a leaked database credential: rotate at the database, then in the Secret,
   then restart.

## See also

- [Security: authentication](../security/authentication.md)
- [SECURITY.md](../../SECURITY.md)
