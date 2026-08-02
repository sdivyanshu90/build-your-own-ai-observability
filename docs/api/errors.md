# Errors

## The envelope

Every error, from every endpoint, has the same shape:

```json
{
  "code": "validation_failed",
  "message": "unknown sort field 'cost:desc' for traces; sortable: [...]",
  "request_id": "req_8f3a2b1c9d4e5f60",
  "details": [
    {
      "location": "sort",
      "message": "unknown field",
      "reason": "unknown_field"
    }
  ],
  "retry_after_seconds": null,
  "context": {},
  "documentation_url": "https://.../docs/api/errors.md#validation_failed"
}
```

`request_id` is the important field. It appears in the response header, in the
envelope, and in every log line for that request.

## Codes

| Code                     | Status | Meaning                                                                  | Retry?                    |
| ------------------------ | ------ | ------------------------------------------------------------------------ | ------------------------- |
| `validation_failed`      | 422    | The request is well-formed but a value is invalid. `details` says which. | No — fix the request      |
| `malformed_request`      | 400    | Not parseable as the declared content type                               | No                        |
| `unauthenticated`        | 401    | No credential, or an invalid one                                         | No — sign in again        |
| `token_expired`          | 401    | The token expired or its epoch was bumped                                | Refresh, then retry       |
| `invalid_credentials`    | 401    | Wrong email or password. Deliberately identical for an unknown account.  | No                        |
| `permission_denied`      | 403    | Authenticated, but the role lacks the permission                         | No                        |
| `not_found`              | 404    | No such resource — or it belongs to another tenant                       | No                        |
| `conflict`               | 409    | Uniqueness or state conflict                                             | Depends                   |
| `payload_too_large`      | 413    | Body over `MAX_REQUEST_BYTES`, or batch over the span limit              | No — send smaller batches |
| `unsupported_media_type` | 415    |                                                                          | No                        |
| `rate_limited`           | 429    | Over the limit. `retry_after_seconds` is set.                            | **Yes**, after the delay  |
| `dependency_unavailable` | 503    | A required dependency is unreachable                                     | **Yes**, with backoff     |
| `timeout`                | 504    | The operation exceeded its budget                                        | **Yes**                   |
| `internal_error`         | 500    | A bug. Quote the request id.                                             | Maybe                     |

## Retryable versus not

The SDKs and the web client both classify:

```
retryable = rate_limited | internal_error | dependency_unavailable | timeout
```

Everything else is a client error and retrying it produces the same response
with more load.

## Partial success on ingest

Ingest is **not** all-or-nothing. One malformed span does not fail a batch of
two thousand:

```json
{
  "accepted": 1998,
  "rejected": 2,
  "results": [
    { "span_id": "a1b2...", "status": "accepted" },
    { "span_id": "c3d4...", "status": "duplicate" },
    {
      "span_id": "e5f6...",
      "status": "rejected",
      "reason": "end_time precedes start_time"
    }
  ]
}
```

Status is 202. The per-span results say what happened to each.

## 404 versus 403

Fetching a resource that exists but belongs to another organisation returns
**404**. A 403 would confirm the resource exists, turning an id into an oracle.

Within your own tenant, 403 means what it says: it exists, and your role cannot
have it.

## Errors are readable cross-origin

An unhandled exception is caught by application middleware and returned as the
standard envelope, so it still travels back out through the CORS layer.

This matters more than it sounds. Starlette's own error handler sits _outside_
every application middleware, so a 500 produced there reaches a browser with no
`Access-Control-Allow-Origin` header — the fetch fails with an opaque "Failed to
fetch" and the request id, the one thing that makes a 500 diagnosable, never
reaches the operator.

## See also

- [Filtering and sorting](filtering.md)
- [Pagination](pagination.md)
