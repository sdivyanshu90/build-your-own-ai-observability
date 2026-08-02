#!/usr/bin/env python
"""Demo 3: a multi-step agent with tools, retries, branching and approval.

Produces the trajectory shapes the graph view has to render:

* sequential decision -> tool -> observation steps
* a tool that times out and is retried (a retry edge)
* a conditional branch (a refund path and an escalation path)
* a human approval step that sometimes times out
* a handoff to a second agent
* explicit termination reasons, including hitting the step budget

The loop is bounded twice over -- by ``max_steps`` and by a repeated-action
check -- because an agent that can loop forever is an agent that can bill
forever, and "why did this cost $400" is a question the trace has to answer.

Run::

    python demos/multi-step-agent/main.py
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiobs  # noqa: E402
from aiobs.integrations.retrieval import AgentRecorder  # noqa: E402
from _shared.mock_provider import MockProvider, ProviderError  # noqa: E402

MAX_STEPS = 8

GOALS = (
    "Resolve the customer's refund request for order ORD-10231",
    "Investigate a duplicate charge on order ORD-88120",
    "Escalate a damaged delivery for order ORD-55019",
)


class Tools:
    """Deterministic tools, some of which fail on purpose."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._calls: dict[str, int] = {}

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self._calls[name] = self._calls.get(name, 0) + 1
        attempt = self._calls[name]

        if name == "search_orders":
            return {"order_id": arguments.get("order_id"), "status": "delivered", "total": "84.00"}
        if name == "lookup_customer":
            return {"customer_id": "CUST-4412", "tier": "growth", "lifetime_value": "1240.00"}
        if name == "issue_refund":
            # Fails the first time, succeeds on the retry: exactly the shape a
            # retry edge in the trajectory graph is meant to show.
            if attempt == 1:
                raise TimeoutError("payment gateway did not respond within 5s")
            return {"refund_id": "REF-9981", "amount": "84.00", "status": "issued"}
        if name == "escalate_to_human":
            return {"ticket_id": "TIC-3320", "queue": "tier-2"}
        if name == "send_email":
            return {"message_id": "MSG-7781", "delivered": True}
        raise ValueError(f"unknown tool {name!r}")


def run_agent(
    client: aiobs.Client,
    goal: str,
    *,
    provider: MockProvider,
    rng: random.Random,
    force_approval_timeout: bool = False,
    force_step_budget: bool = False,
) -> str:
    """Run one agent trajectory and return its termination reason."""
    with client.trace("multi-step-agent", tags=["demo", "agent"]) as trace:
        trace.set_input(goal)
        tools = Tools(rng)
        agent = AgentRecorder(
            agent_id="support-agent", goal=goal, agent_version="2.1.0", max_steps=MAX_STEPS
        )
        # Deliberately more work than the budget allows when force_step_budget
        # is set, so the max-steps termination path is exercised.
        plan = (
            ["search_orders", "lookup_customer", "issue_refund", "send_email"]
            if not force_step_budget
            else ["search_orders", "lookup_customer", "issue_refund", "send_email"] * 3
        )
        needs_escalation = "damaged" in goal or "duplicate" in goal
        if needs_escalation:
            agent.branch("escalation")
            plan = ["search_orders", "escalate_to_human"]
        else:
            agent.branch("refund")

        seen_actions: list[str] = []

        for tool_name in plan:
            if agent.budget_exhausted:
                agent.terminate("max_steps")
                trace.set_tags("terminated-max-steps")
                return "max_steps"

            # Loop guard: three identical actions in a row means the agent is
            # stuck, and continuing only costs money.
            seen_actions.append(tool_name)
            if len(seen_actions) >= 3 and len(set(seen_actions[-3:])) == 1:
                agent.terminate("loop_detected")
                return "loop_detected"

            with agent.step(f"decide[{tool_name}]") as span:
                span.record_model(provider=provider.provider, model=provider.model, temperature=0.0)
                messages = [{"role": "user", "content": f"{goal}. Next tool?"}]
                response = provider.complete(messages)
                span.record_usage(
                    input_tokens=response.usage["prompt_tokens"],
                    output_tokens=response.usage["completion_tokens"],
                    raw=response.usage,
                )
                span.record_agent_step(
                    **agent.decision(summary=f"Call {tool_name} to make progress on the goal")
                )

            arguments = {"order_id": goal.split()[-1]}
            with agent.step(f"tool.{tool_name}", category="tool_call") as span:
                try:
                    result = tools.call(tool_name, arguments)
                    span.set_output(result)
                    span.record_agent_step(
                        **agent.tool_call(tool=tool_name, args=arguments, status="ok")
                    )
                except TimeoutError as exc:
                    span.record_exception(exc)
                    span.record_agent_step(
                        **agent.tool_call(tool=tool_name, args=arguments, status="timeout")
                    )
                    failed_step = agent.current_step

                    # Retry once. The retry_of link is what draws the retry edge.
                    with agent.step(f"tool.{tool_name}(retry)", category="tool_call") as retry:
                        result = tools.call(tool_name, arguments)
                        retry.set_output(result)
                        retry.record_agent_step(
                            **agent.tool_call(
                                tool=tool_name,
                                args=arguments,
                                status="ok",
                                retry_of=failed_step,
                            )
                        )

            if tool_name == "escalate_to_human":
                status = "timeout" if force_approval_timeout else "approved"
                with agent.step("human-approval", category="agent_decision") as span:
                    span.record_agent_step(**agent.approval(status=status))
                    span.set_attribute("aiobs.agent.approval.waited_seconds", 12 if status == "approved" else 300)
                if status == "timeout":
                    agent.terminate("approval_timeout")
                    trace.set_tags("approval-timeout")
                    return "approval_timeout"

                # Hand off to a second agent, which is what makes this a
                # multi-agent trajectory rather than one long chain.
                with agent.step("handoff", category="agent_handoff") as span:
                    span.record_agent_step(
                        **agent.handoff(
                            target="billing-agent", summary="Tier-2 billing review required"
                        )
                    )
                billing = AgentRecorder(
                    agent_id="billing-agent", goal="Review the escalated case", max_steps=3
                )
                with billing.step("decide[review]") as span:
                    span.record_agent_step(**billing.decision(summary="Approve the goodwill credit"))
                with billing.step("tool.issue_credit", category="tool_call") as span:
                    span.record_agent_step(
                        **billing.tool_call(tool="issue_credit", args={"amount": "20.00"})
                    )
                billing.terminate("completed")

        agent.terminate("completed")
        trace.set_output("resolved")
        return "completed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-step agent demo")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--seed", type=int, default=17)
    arguments = parser.parse_args(argv)

    client = aiobs.init(service_name="agent-demo", service_version="2.1.0")
    provider = MockProvider()
    rng = random.Random(arguments.seed)
    print(f"endpoint={client.config.endpoint} authenticated={bool(client.config.api_key)}")

    outcomes: dict[str, int] = {}
    for iteration in range(arguments.iterations):
        for index, goal in enumerate(GOALS):
            reason = run_agent(
                client,
                goal,
                provider=provider,
                rng=rng,
                # Rotate the deliberate failure modes so a single run produces
                # every termination reason the UI needs to render.
                force_approval_timeout=(index == 2),
                force_step_budget=(index == 1 and iteration == 0),
            )
            outcomes[reason] = outcomes.get(reason, 0) + 1
            print(f"  {goal[:52]:54} -> {reason}")

    client.shutdown()
    print(f"\ntermination reasons: {outcomes}")
    print(f"exporter: {client.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
