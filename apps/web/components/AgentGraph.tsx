"use client";

/**
 * Agent trajectory visualisation.
 *
 * An agent run is a DAG, not a list: it branches, retries, loops and hands off
 * between agents. Rendering it as a flat step list hides exactly the structure
 * you need when debugging "why did it take 14 steps".
 *
 * Layout is deterministic and computed here rather than by a force simulation:
 * the same trajectory must produce the same picture every time it is opened,
 * otherwise screenshots in incident reports are worthless. Rows are ordered by
 * step number; columns are lanes assigned per branch, with the primary branch
 * pinned to lane 0 so the happy path is always the leftmost column.
 *
 * Nothing here renders hidden chain-of-thought. `decision_summary` is the
 * short, user-approved rationale the SDK records; if an application never sets
 * it the node simply shows its observable action.
 */

import { useMemo, useState } from "react";
import type {
  AgentGraph as AgentGraphData,
  AgentGraphEdge,
  AgentGraphNode,
} from "@aiobs/schemas";

import { formatCost, formatDuration, formatNumber } from "@/lib/format";
import {
  Card,
  EmptyState,
  PartialDataNotice,
  SafeText,
  StatusBadge,
} from "./ui";

const NODE_WIDTH = 208;
const NODE_HEIGHT = 76;
const COLUMN_GAP = 40;
const ROW_GAP = 32;
const PADDING = 16;

interface Placed {
  node: AgentGraphNode;
  x: number;
  y: number;
}

/** Edge styling by relationship kind. Dash patterns carry the meaning so the
 *  graph stays readable in greyscale and for colour-blind readers; colour is
 *  only reinforcement. */
const EDGE_STYLE: Record<
  AgentGraphEdge["kind"],
  { stroke: string; dash?: string; label: string }
> = {
  sequence: { stroke: "var(--border-strong)", label: "then" },
  branch: { stroke: "var(--accent)", dash: "6 3", label: "branches to" },
  retry: { stroke: "var(--warn)", dash: "2 3", label: "retries as" },
  handoff: { stroke: "var(--info)", dash: "10 4", label: "hands off to" },
  loop: { stroke: "var(--error)", dash: "1 4", label: "loops back to" },
};

export function AgentGraphView({ graph }: { graph: AgentGraphData }) {
  const [selectedId, setSelectedId] = useState<string | null>(
    graph.nodes[0]?.id ?? null,
  );

  const layout = useMemo(() => computeLayout(graph), [graph]);
  const selected = graph.nodes.find((node) => node.id === selectedId) ?? null;

  if (graph.nodes.length === 0) {
    return (
      <EmptyState
        title="No agent steps in this trace"
        description="Steps appear when spans record aiobs.agent.step_number. Use agent_span() / tool_span() in the SDK, or set the attributes directly."
      />
    );
  }

  return (
    <div
      style={{
        display: "grid",
        gap: "1rem",
        gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)",
      }}
    >
      <Card
        title="Trajectory"
        subtitle={`${graph.total_steps} step(s) · ${graph.agents.length} agent(s) · ${graph.branches.length} branch(es)`}
      >
        <Summary graph={graph} />
        {graph.truncated && (
          <PartialDataNotice reason="This trajectory was truncated for display. Some later steps are not shown." />
        )}
        {graph.loop_detected && (
          <PartialDataNotice reason="A loop was detected: the agent revisited a step it had already performed. Look for a missing termination condition." />
        )}
        <div style={{ overflow: "auto", marginTop: "1rem" }}>
          <svg
            role="img"
            aria-label={`Agent trajectory with ${graph.nodes.length} steps`}
            width={layout.width}
            height={layout.height}
            style={{ minWidth: "100%", display: "block" }}
          >
            <defs>
              {Object.entries(EDGE_STYLE).map(([kind, style]) => (
                <marker
                  key={kind}
                  id={`arrow-${kind}`}
                  viewBox="0 0 8 8"
                  refX="7"
                  refY="4"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 8 4 L 0 8 z" fill={style.stroke} />
                </marker>
              ))}
            </defs>

            {layout.edges.map((edge, index) => (
              <EdgePath
                key={`${edge.edge.source}-${edge.edge.target}-${index}`}
                {...edge}
              />
            ))}

            {layout.nodes.map(({ node, x, y }) => (
              <GraphNode
                key={node.id}
                node={node}
                x={x}
                y={y}
                selected={node.id === selectedId}
                onSelect={() => setSelectedId(node.id)}
              />
            ))}
          </svg>
        </div>
        <Legend />
      </Card>

      <StepDetail node={selected} graph={graph} />
    </div>
  );
}

function Summary({ graph }: { graph: AgentGraphData }) {
  const items: [string, string][] = [
    [
      "Steps",
      `${graph.total_steps}${graph.max_steps ? ` / ${graph.max_steps} max` : ""}`,
    ],
    ["Retries", String(graph.retry_count)],
    ["Handoffs", String(graph.handoff_count)],
    ["Termination", graph.termination_reason || "not recorded"],
  ];
  return (
    <dl
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(7rem, 1fr))",
        gap: "0.75rem",
        margin: 0,
      }}
    >
      {items.map(([label, value]) => (
        <div key={label}>
          <dt style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}>
            {label}
          </dt>
          <dd
            style={{
              margin: "0.125rem 0 0",
              fontSize: "0.9375rem",
              fontWeight: 600,
            }}
          >
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function GraphNode({
  node,
  x,
  y,
  selected,
  onSelect,
}: {
  node: AgentGraphNode;
  x: number;
  y: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const status = nodeStatus(node);
  return (
    <g
      transform={`translate(${x}, ${y})`}
      role="button"
      tabIndex={0}
      aria-label={`Step ${node.step_number}: ${node.label}, ${status}`}
      aria-pressed={selected}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
      style={{ cursor: "pointer" }}
    >
      <rect
        width={NODE_WIDTH}
        height={NODE_HEIGHT}
        rx={8}
        fill={
          node.on_critical_path ? "var(--accent-subtle)" : "var(--bg-raised)"
        }
        stroke={selected ? "var(--accent)" : statusStroke(status)}
        strokeWidth={selected ? 2 : 1}
      />
      {node.is_retry && (
        <rect
          width={4}
          height={NODE_HEIGHT}
          rx={2}
          fill="var(--warn)"
          aria-hidden="true"
        />
      )}
      <text x={12} y={20} fontSize={11} fill="var(--text-muted)">
        {node.step_number}. {node.step_type}
        {node.loop_iteration !== null ? ` · iter ${node.loop_iteration}` : ""}
      </text>
      <text x={12} y={38} fontSize={13} fontWeight={600} fill="var(--text)">
        {truncate(node.label || node.tool_name || node.step_type, 26)}
      </text>
      <text x={12} y={56} fontSize={11} fill="var(--text-muted)">
        {node.agent_id ? `${truncate(node.agent_id, 14)} · ` : ""}
        {node.duration_ms !== null
          ? formatDuration(node.duration_ms)
          : "no duration"}
      </text>
      <text x={12} y={70} fontSize={10} fill={statusStroke(status)}>
        {statusGlyph(status)} {status}
        {node.approval_status && node.approval_status !== "not_required"
          ? ` · approval ${node.approval_status}`
          : ""}
      </text>
    </g>
  );
}

function EdgePath({
  edge,
  from,
  to,
}: {
  edge: AgentGraphEdge;
  from: Placed;
  to: Placed;
}) {
  const style = EDGE_STYLE[edge.kind] ?? EDGE_STYLE.sequence;
  const x1 = from.x + NODE_WIDTH / 2;
  const y1 = from.y + NODE_HEIGHT;
  const x2 = to.x + NODE_WIDTH / 2;
  const y2 = to.y;

  // A loop edge points backwards; route it around the left margin so it never
  // hides behind the nodes it connects.
  const path =
    y2 < y1
      ? `M ${x1} ${from.y + NODE_HEIGHT / 2} C ${x1 - 90} ${from.y}, ${x2 - 90} ${to.y + NODE_HEIGHT}, ${x2} ${to.y + NODE_HEIGHT / 2}`
      : `M ${x1} ${y1} C ${x1} ${y1 + ROW_GAP / 2}, ${x2} ${y2 - ROW_GAP / 2}, ${x2} ${y2}`;

  return (
    <g>
      <title>{`${style.label}: ${edge.label || edge.kind}`}</title>
      <path
        d={path}
        fill="none"
        stroke={style.stroke}
        strokeWidth={1.5}
        strokeDasharray={style.dash}
        markerEnd={`url(#arrow-${edge.kind})`}
      />
      {edge.label && (
        <text
          x={(x1 + x2) / 2 + 6}
          y={(y1 + y2) / 2}
          fontSize={10}
          fill="var(--text-faint)"
        >
          {truncate(edge.label, 18)}
        </text>
      )}
    </g>
  );
}

function Legend() {
  return (
    <ul
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: "0.75rem",
        listStyle: "none",
        padding: 0,
        margin: "0.75rem 0 0",
        fontSize: "0.6875rem",
        color: "var(--text-muted)",
      }}
    >
      {(
        Object.entries(EDGE_STYLE) as [
          AgentGraphEdge["kind"],
          (typeof EDGE_STYLE)["sequence"],
        ][]
      ).map(([kind, style]) => (
        <li
          key={kind}
          style={{ display: "flex", alignItems: "center", gap: "0.375rem" }}
        >
          <svg width="26" height="8" aria-hidden="true">
            <line
              x1="0"
              y1="4"
              x2="26"
              y2="4"
              stroke={style.stroke}
              strokeWidth="1.5"
              strokeDasharray={style.dash}
            />
          </svg>
          {kind}
        </li>
      ))}
    </ul>
  );
}

function StepDetail({
  node,
  graph,
}: {
  node: AgentGraphNode | null;
  graph: AgentGraphData;
}) {
  if (!node) {
    return (
      <EmptyState
        title="Select a step"
        description="Choose a node to see its inputs, cost and outcome."
      />
    );
  }
  const incoming = graph.edges.filter((edge) => edge.target === node.id);
  const outgoing = graph.edges.filter((edge) => edge.source === node.id);

  return (
    <Card
      title={`Step ${node.step_number}`}
      subtitle={node.label || node.step_type}
    >
      <dl style={{ margin: 0, display: "grid", gap: "0.5rem" }}>
        <Row label="Agent" value={node.agent_id || "—"} />
        <Row label="Type" value={node.step_type} />
        {node.tool_name && (
          <Row
            label="Tool"
            value={`${node.tool_name} (${node.tool_status || "unknown"})`}
          />
        )}
        <Row label="Status" value={<StatusBadge status={nodeStatus(node)} />} />
        <Row
          label="Duration"
          value={
            node.duration_ms === null ? "—" : formatDuration(node.duration_ms)
          }
        />
        <Row label="Branch" value={node.branch_id || "primary"} />
        {node.is_retry && (
          <Row
            label="Retry"
            value="this step is a retry of an earlier attempt"
          />
        )}
        {node.loop_iteration !== null && (
          <Row label="Loop iteration" value={String(node.loop_iteration)} />
        )}
        {node.approval_status && (
          <Row label="Approval" value={node.approval_status} />
        )}
        {node.termination_reason && (
          <Row label="Termination" value={node.termination_reason} />
        )}
        <Row
          label="Tokens"
          value={
            node.input_tokens === null && node.output_tokens === null
              ? "not recorded"
              : `${formatNumber(node.input_tokens)} in / ${formatNumber(node.output_tokens)} out`
          }
        />
        <Row label="Cost" value={formatCost(node.cost_total, "USD")} />
        <Row
          label="Span"
          value={<code style={{ fontSize: "0.6875rem" }}>{node.span_id}</code>}
        />
      </dl>

      {node.decision_summary && (
        <section style={{ marginTop: "0.75rem" }}>
          <h4
            style={{
              margin: "0 0 0.25rem",
              fontSize: "0.75rem",
              color: "var(--text-muted)",
            }}
          >
            Decision summary
          </h4>
          <SafeText maxLines={6}>{node.decision_summary}</SafeText>
          <p
            style={{
              margin: "0.25rem 0 0",
              fontSize: "0.6875rem",
              color: "var(--text-faint)",
            }}
          >
            Recorded by the application. The platform never stores hidden
            reasoning.
          </p>
        </section>
      )}

      {node.error_message && (
        <section style={{ marginTop: "0.75rem" }}>
          <h4
            style={{
              margin: "0 0 0.25rem",
              fontSize: "0.75rem",
              color: "var(--error)",
            }}
          >
            Error
          </h4>
          <SafeText maxLines={6}>{node.error_message}</SafeText>
        </section>
      )}

      {(incoming.length > 0 || outgoing.length > 0) && (
        <section style={{ marginTop: "0.75rem", fontSize: "0.75rem" }}>
          <h4
            style={{
              margin: "0 0 0.25rem",
              fontSize: "0.75rem",
              color: "var(--text-muted)",
            }}
          >
            Connections
          </h4>
          <ul style={{ margin: 0, paddingLeft: "1rem" }}>
            {incoming.map((edge, index) => (
              <li key={`in-${index}`}>
                from {edge.source} ({edge.kind})
              </li>
            ))}
            {outgoing.map((edge, index) => (
              <li key={`out-${index}`}>
                to {edge.target} ({edge.kind})
              </li>
            ))}
          </ul>
        </section>
      )}
    </Card>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        gap: "0.75rem",
        fontSize: "0.8125rem",
      }}
    >
      <dt style={{ color: "var(--text-muted)" }}>{label}</dt>
      <dd style={{ margin: 0, textAlign: "right", wordBreak: "break-word" }}>
        {value}
      </dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// layout
// ---------------------------------------------------------------------------

/**
 * Assign every node a (row, lane) slot.
 *
 * Rows follow step order so time always runs downwards. Lanes are per branch,
 * with the branch containing step 1 pinned to lane 0. Two nodes never share a
 * slot: if a branch emits several steps with the same number -- which happens
 * with parallel tool calls -- the later ones spill into free lanes rather than
 * overlapping.
 */
export function computeLayout(graph: AgentGraphData): {
  nodes: Placed[];
  edges: { edge: AgentGraphEdge; from: Placed; to: Placed }[];
  width: number;
  height: number;
} {
  const ordered = [...graph.nodes].sort(
    (a, b) => a.step_number - b.step_number || a.id.localeCompare(b.id),
  );

  const laneOf = new Map<string, number>();
  const primaryBranch = ordered[0]?.branch_id ?? "";
  laneOf.set(primaryBranch, 0);
  for (const node of ordered) {
    const branch = node.branch_id ?? "";
    if (!laneOf.has(branch)) laneOf.set(branch, laneOf.size);
  }

  const occupied = new Set<string>();
  const placed: Placed[] = [];
  const byId = new Map<string, Placed>();

  ordered.forEach((node, index) => {
    const row = index === 0 ? 0 : rowFor(node, ordered);
    let lane = laneOf.get(node.branch_id ?? "") ?? 0;
    while (occupied.has(`${row}:${lane}`)) lane += 1;
    occupied.add(`${row}:${lane}`);

    const entry: Placed = {
      node,
      x: PADDING + lane * (NODE_WIDTH + COLUMN_GAP),
      y: PADDING + row * (NODE_HEIGHT + ROW_GAP),
    };
    placed.push(entry);
    byId.set(node.id, entry);
  });

  const edges = graph.edges
    .map((edge) => {
      const from = byId.get(edge.source);
      const to = byId.get(edge.target);
      return from && to ? { edge, from, to } : null;
    })
    .filter(
      (value): value is { edge: AgentGraphEdge; from: Placed; to: Placed } =>
        value !== null,
    );

  const maxLane = Math.max(0, ...placed.map((entry) => entry.x));
  const maxRow = Math.max(0, ...placed.map((entry) => entry.y));

  return {
    nodes: placed,
    edges,
    width: maxLane + NODE_WIDTH + PADDING,
    height: maxRow + NODE_HEIGHT + PADDING,
  };
}

/** Row index derived from step number, densely packed so gaps in numbering
 *  (a sampled-out step, a step recorded by a service that dropped) do not
 *  leave a blank band in the middle of the graph. */
function rowFor(node: AgentGraphNode, ordered: AgentGraphNode[]): number {
  const distinct = [...new Set(ordered.map((item) => item.step_number))].sort(
    (a, b) => a - b,
  );
  return Math.max(0, distinct.indexOf(node.step_number));
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

export function nodeStatus(
  node: AgentGraphNode,
): "ok" | "error" | "warn" | "unset" {
  if (
    node.status === "error" ||
    node.tool_status === "error" ||
    node.error_message
  )
    return "error";
  if (node.is_retry || node.approval_status === "rejected") return "warn";
  if (node.status === "ok" || node.tool_status === "ok") return "ok";
  return "unset";
}

function statusStroke(status: string): string {
  if (status === "error") return "var(--error)";
  if (status === "warn") return "var(--warn)";
  if (status === "ok") return "var(--ok)";
  return "var(--border-strong)";
}

function statusGlyph(status: string): string {
  if (status === "error") return "✕";
  if (status === "warn") return "!";
  if (status === "ok") return "✓";
  return "·";
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
