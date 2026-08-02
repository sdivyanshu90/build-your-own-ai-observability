# Instrumenting an agent

The question an agent trace has to answer is "why did this take fourteen
steps?". That means recording the structure — branches, retries, loops, handoffs
— not just the sequence.

## The shape

```python
from aiobs import Client

client = Client(service_name="support-agent")

def run(goal: str, session_id: str) -> str:
    with client.trace("agent-run", session_id=session_id) as trace:
        step = 0
        while not done and step < MAX_STEPS:
            step += 1

            # Decide
            with trace.span(f"step-{step}-plan", category="agent_decision") as span:
                span.record_model(provider="anthropic", model="claude-sonnet-4")
                action = plan(state)
                span.record_agent_step(
                    step_number=step,
                    step_type="plan",
                    agent_id="planner",
                    decision_summary=action.rationale,   # short, deliberate
                )
                span.record_usage_from(action.response)

            # Act
            with trace.span(f"step-{step}-{action.tool}", category="tool_call") as span:
                span.record_agent_step(
                    step_number=step,
                    step_type="tool_call",
                    agent_id="planner",
                    tool_name=action.tool,
                    tool_status="ok",
                    is_retry=action.is_retry,
                    branch_id=action.branch,
                    loop_iteration=action.iteration,
                )
                state = execute(action)

        with trace.span("terminate", category="agent_decision") as span:
            span.record_agent_step(
                step_number=step + 1,
                step_type="terminate",
                termination_reason="answered" if done else "step_limit",
            )
    return state.answer
```

## Fields that make the graph readable

| Field                | What it enables                                        |
| -------------------- | ------------------------------------------------------ |
| `step_number`        | Time ordering. Rows in the graph.                      |
| `branch_id`          | Lanes. Parallel exploration draws as parallel columns. |
| `is_retry`           | Retries group in the waterfall and draw a retry edge   |
| `loop_iteration`     | Loop detection, and a backwards edge                   |
| `agent_id`           | Handoffs between agents become visible as handoffs     |
| `termination_reason` | "Finished" versus "hit the wall"                       |

## `decision_summary`, and what not to put in it

One or two sentences, written by your application, appropriate for a user to
read. "Search the knowledge base before answering." "The order id looks
malformed; ask the user to confirm."

**Not** the model's raw reasoning. There is no field for it, no attribute in the
registry, and no UI that would render it. That is deliberate — see
[concepts/agent-trajectories.md](../concepts/agent-trajectories.md).

If your framework exposes a reasoning trace, summarise it or drop it.

## Human approval

```python
span.record_agent_step(
    step_number=step,
    step_type="tool_call",
    tool_name="issue_refund",
    approval_status="pending",     # then approved | rejected
)
```

Approval status appears on the node. An agent that took an irreversible action
without approval is a specific, findable thing rather than a story you piece
together.

## Sub-agents

A sub-agent gets its own trace, **linked** rather than nested:

```python
with trace.span("delegate-to-researcher", category="agent_handoff") as span:
    with client.trace("researcher-run", parent=span.context) as sub:
        ...
    span.add_link(sub.context, role="sub_agent")
```

A link is not a parent. Nesting a sub-agent's trace inside its caller's makes
the caller's duration include work that ran in a different process, and the
waterfall then lies.

## Reading a slow run

1. **Trajectory** tab. Count the steps against `max_steps`.
2. Look for retry edges. Three retries of the same tool is a tool problem, not
   an agent problem.
3. Look for a loop edge. A revisited step is almost always a missing termination
   condition.
4. Check `termination_reason`. `step_limit` means it never actually finished.
5. Back to the **waterfall** for where the time went. The critical path is
   marked; anything off it cannot be the cause.

## See also

- [Agent trajectories](../concepts/agent-trajectories.md)
- [Instrumenting a RAG pipeline](instrumenting-rag.md)
