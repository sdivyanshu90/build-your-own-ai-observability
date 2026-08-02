# ADR-0012: No storage for hidden chain-of-thought

## Status

Accepted.

## Context

Reasoning models emit internal chain-of-thought that is not shown to the user.
It is genuinely useful for debugging: it says why the model chose the tool it
chose.

It is also the most sensitive text an agent produces — unfiltered speculation
about the user, the data and the task — and several providers now forbid
retaining it.

## Decision

The platform provides **no field for hidden reasoning**, no attribute in the
registry, and no UI that would render it.

Agent steps record `decision_summary`: a short, application-authored rationale,
appropriate for a user to read, and the observable action taken.

## Consequences

**Good.** The platform cannot leak what it does not store. No provider terms are
violated by using it. There is no retention question about reasoning text,
because there is no reasoning text. `decision_summary` is written deliberately
by the application, which means it is fit to show a user — including in a
support conversation.

**Costs.** Some debugging is harder. When a model chose a strange tool, its
reasoning would have said why, and instead you have the summary the application
wrote. Teams who want that must summarise deliberately, which is friction.

That friction is the point. An unbounded, unfiltered, sensitive text field that
every framework fills automatically is not a feature you can add safely and
remove later.

## What applications should record instead

```python
span.record_agent_step(
    step_number=3,
    step_type="tool_call",
    tool_name="issue_refund",
    decision_summary="Order is within the 30-day window and the item is unused.",
)
```

One or two sentences. If your framework exposes a reasoning trace, summarise it
or drop it.

## Alternatives considered

**Store it behind a feature flag.** Flags get enabled. A flag that is off by
default and on in half the deployments provides the risk with none of the
clarity.

**Store it with a short retention.** Still stored, still readable by an
administrator, still a provider-terms question. Short retention reduces the
window, not the exposure.

**Store a hash.** Useless — you cannot read a hash to debug — and it still
proves the platform received the text.

**Store it encrypted with a customer-held key.** Genuinely defensible, and a
large amount of key-management machinery for a feature whose value is
"occasionally useful when debugging". Revisitable if demand is real.
