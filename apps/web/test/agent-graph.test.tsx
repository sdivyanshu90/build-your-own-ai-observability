import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  AgentGraphView,
  computeLayout,
  nodeStatus,
} from "@/components/AgentGraph";
import { makeAgentGraph } from "./fixtures";

describe("computeLayout", () => {
  it("is deterministic: the same graph always produces the same coordinates", () => {
    const graph = makeAgentGraph();
    const first = computeLayout(graph);
    const second = computeLayout(makeAgentGraph());
    expect(
      first.nodes.map((entry) => [entry.node.id, entry.x, entry.y]),
    ).toEqual(second.nodes.map((entry) => [entry.node.id, entry.x, entry.y]));
  });

  it("orders rows by step number so time runs downwards", () => {
    const layout = computeLayout(makeAgentGraph());
    const ys = layout.nodes.map((entry) => entry.y);
    expect(ys).toEqual([...ys].sort((a, b) => a - b));
  });

  it("pins the branch containing step 1 to the leftmost lane", () => {
    const layout = computeLayout(makeAgentGraph());
    const first = layout.nodes.find((entry) => entry.node.id === "step-1");
    const handoff = layout.nodes.find((entry) => entry.node.id === "step-4");
    expect(first?.x).toBeLessThan(handoff!.x);
  });

  it("never places two nodes in the same slot", () => {
    // Two parallel tool calls recorded with the same step number.
    const graph = makeAgentGraph();
    const parallel = {
      ...graph,
      nodes: [
        ...graph.nodes,
        {
          ...graph.nodes[1]!,
          id: "step-2b",
          label: "lookup_order",
          span_id: "span-2b",
        },
      ],
    };
    const layout = computeLayout(parallel);
    const slots = layout.nodes.map((entry) => `${entry.x}:${entry.y}`);
    expect(new Set(slots).size).toBe(slots.length);
  });

  it("drops edges that reference a node not present in the graph", () => {
    const graph = makeAgentGraph();
    const layout = computeLayout({
      ...graph,
      edges: [
        ...graph.edges,
        { source: "step-1", target: "ghost", kind: "sequence", label: "" },
      ],
    });
    expect(layout.edges).toHaveLength(graph.edges.length);
  });

  it("handles an empty graph without producing negative dimensions", () => {
    const layout = computeLayout(makeAgentGraph({ nodes: [], edges: [] }));
    expect(layout.nodes).toHaveLength(0);
    expect(layout.width).toBeGreaterThan(0);
    expect(layout.height).toBeGreaterThan(0);
  });
});

describe("nodeStatus", () => {
  it("treats a recorded error message as an error even when status says otherwise", () => {
    const node = {
      ...makeAgentGraph().nodes[0]!,
      status: "ok",
      error_message: "boom",
    };
    expect(nodeStatus(node)).toBe("error");
  });

  it("marks a retry as a warning rather than a success", () => {
    const node = { ...makeAgentGraph().nodes[2]! };
    expect(nodeStatus(node)).toBe("warn");
  });
});

describe("AgentGraphView", () => {
  it("explains how to instrument when there are no steps", () => {
    render(
      <AgentGraphView
        graph={makeAgentGraph({ nodes: [], edges: [], total_steps: 0 })}
      />,
    );
    expect(
      screen.getByText("No agent steps in this trace"),
    ).toBeInTheDocument();
  });

  it("summarises retries, handoffs and the termination reason", () => {
    render(<AgentGraphView graph={makeAgentGraph()} />);
    expect(screen.getByText("4 / 10 max")).toBeInTheDocument();
    expect(screen.getByText("answered")).toBeInTheDocument();
  });

  it("renders one selectable node per step", () => {
    render(<AgentGraphView graph={makeAgentGraph()} />);
    const nodes = screen.getAllByRole("button", { name: /^Step \d/ });
    expect(nodes).toHaveLength(4);
  });

  it("shows the detail of the step you select", () => {
    render(<AgentGraphView graph={makeAgentGraph()} />);
    fireEvent.click(
      screen.getByRole("button", { name: /Step 2: search_docs/ }),
    );
    expect(screen.getByText("upstream timeout")).toBeInTheDocument();
  });

  it("labels the decision summary as application-recorded and disclaims hidden reasoning", () => {
    render(<AgentGraphView graph={makeAgentGraph()} />);
    expect(
      screen.getByText(/never stores hidden reasoning/),
    ).toBeInTheDocument();
  });

  it("warns when a loop was detected", () => {
    render(<AgentGraphView graph={makeAgentGraph({ loop_detected: true })} />);
    expect(
      screen.getByText(/missing termination condition/),
    ).toBeInTheDocument();
  });

  it("says so when the trajectory was truncated", () => {
    render(<AgentGraphView graph={makeAgentGraph({ truncated: true })} />);
    expect(screen.getByText(/truncated for display/)).toBeInTheDocument();
  });

  it("distinguishes edge kinds by dash pattern, not colour alone", () => {
    const { container } = render(<AgentGraphView graph={makeAgentGraph()} />);
    const dashed = container.querySelectorAll("path[stroke-dasharray]");
    expect(dashed.length).toBeGreaterThan(0);
  });
});
