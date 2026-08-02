# ADR-0003: Content-addressed immutable versions with movable aliases

## Status

Accepted.

## Context

"It was fine last week" is unanswerable without knowing what "last week" was
running. That requires a trace to reference the exact prompt and model
configuration that produced it, months later.

Two obvious models: incrementing version numbers, or mutable named
configurations.

## Decision

A version is **immutable** and identified by the SHA-256 of the canonical JSON
of its behaviour-defining fields. An alias (`production`, `staging`) is a
**movable pointer**. Traces record the version id, never the alias.

## Consequences

**Good.** A trace from March resolves to exactly the text that produced it.
Registering identical content twice returns the same version rather than a
duplicate, so re-running a deploy script is free. Comparing two traces reduces
to comparing two hashes. Rollback is promoting an alias to an older id — the
same operation as deploying.

**Costs.** Version ids are opaque hashes, not "v3", so the UI has to show
version _numbers_ alongside them for humans. Changing a description creates no
new version, which surprises people until they understand that descriptions do
not change what the model sees. Hashing requires a canonical serialisation,
which is [ADR-0004](0004-canonical-json.md) and is more work than it looks.

## Alternatives considered

**Incrementing version numbers.** Simple, and does not converge: registering
identical content twice creates v4 and v5 with the same text, and comparing
versions means comparing content rather than ids.

**Mutable named configurations.** "The production prompt" is then a moving
target, and a trace that recorded "used the production prompt" becomes
meaningless the moment production moves. This is the model that makes the
original question unanswerable.

**Git as the registry.** Tempting — content addressing is what git does. But it
puts a git operation on the request path for alias resolution, requires every
service to have repository access, and makes multi-tenancy a directory
convention.
