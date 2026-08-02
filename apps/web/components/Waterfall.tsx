"use client";

/**
 * Span waterfall.
 *
 * The central view of the product. Design decisions that matter:
 *
 * **The critical path is highlighted, not just drawn.** A waterfall with fifty
 * bars tells you what happened; it does not tell you what to fix. The critical
 * path -- the chain that actually determined total latency -- does.
 *
 * **Self time is shown alongside total time.** Total duration attributes a slow
 * child to its parent, which makes an orchestration span look expensive when
 * the cost is one nested provider call.
 *
 * **Deep trees collapse.** A thousand-span agent trace is unreadable expanded
 * and slow to render. Subtrees collapse, and the collapsed state summarises
 * what is hidden rather than just hiding it.
 *
 * **Rows are keyboard navigable.** Arrow keys move, Enter selects, Left/Right
 * collapse and expand -- the same idiom as a file tree.
 */

import { useCallback, useMemo, useState } from "react";
import type { Span, TraceDetail } from "@aiobs/schemas";

import { formatCost, formatDuration, formatNumber } from "@/lib/format";
import { StatusBadge } from "./ui";

const CATEGORY_COLOUR: Record<string, string> = {
  chat_completion: "var(--series-1)",
  llm_generation: "var(--series-1)",
  embedding: "var(--series-3)",
  retrieval: "var(--series-3)",
  rerank: "var(--series-6)",
  tool_call: "var(--series-2)",
  agent_decision: "var(--series-4)",
  agent_handoff: "var(--series-4)",
  prompt_render: "var(--series-8)",
  guardrail: "var(--series-7)",
  http_request: "var(--series-5)",
  db_query: "var(--series-5)",
  queue_operation: "var(--series-5)",
  workflow_step: "var(--text-faint)",
  custom: "var(--text-faint)",
};

interface Row {
  span: Span;
  depth: number;
  childCount: number;
  descendantCount: number;
}

/** Flatten the span tree into visible rows, honouring collapsed subtrees. */
function flatten(
  detail: TraceDetail,
  collapsed: ReadonlySet<string>,
): { rows: Row[]; descendants: Map<string, number> } {
  const children = detail.children ?? {};
  const byId = new Map(detail.spans.map((span) => [span.span_id, span]));

  // Count descendants once so a collapsed row can say how much it hides.
  const descendants = new Map<string, number>();
  const countDescendants = (id: string, guard = 0): number => {
    if (guard > 200) return 0;
    const direct = children[id] ?? [];
    let total = direct.length;
    for (const child of direct) total += countDescendants(child, guard + 1);
    descendants.set(id, total);
    return total;
  };

  const roots = children[""] ?? [];
  for (const root of roots) countDescendants(root);

  const rows: Row[] = [];
  const walk = (id: string, depth: number): void => {
    const span = byId.get(id);
    if (!span) return;
    const direct = children[id] ?? [];
    rows.push({
      span,
      depth,
      childCount: direct.length,
      descendantCount: descendants.get(id) ?? 0,
    });
    if (collapsed.has(id)) return;
    for (const child of direct) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, 0);

  return { rows, descendants };
}

export function Waterfall({
  detail,
  selectedSpanId,
  onSelect,
}: {
  detail: TraceDetail;
  selectedSpanId: string | null;
  onSelect: (spanId: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [focusIndex, setFocusIndex] = useState(0);

  const { rows } = useMemo(
    () => flatten(detail, collapsed),
    [detail, collapsed],
  );
  const criticalPath = useMemo(
    () => new Set(detail.critical_path),
    [detail.critical_path],
  );

  const { originNs, spanNs } = useMemo(() => {
    const starts = detail.spans.map((span) =>
      new Date(span.start_time).getTime(),
    );
    const ends = detail.spans.map((span) =>
      span.end_time
        ? new Date(span.end_time).getTime()
        : new Date(span.start_time).getTime(),
    );
    const origin = Math.min(...starts, Number.POSITIVE_INFINITY);
    const finish = Math.max(...ends, Number.NEGATIVE_INFINITY);
    // Never divide by zero: an instantaneous trace still needs a scale.
    return { originNs: origin, spanNs: Math.max(finish - origin, 1) };
  }, [detail.spans]);

  const toggle = useCallback((spanId: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(spanId)) next.delete(spanId);
      else next.add(spanId);
      return next;
    });
  }, []);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent, index: number, row: Row) => {
      const move = (delta: number) => {
        event.preventDefault();
        const next = Math.min(Math.max(index + delta, 0), rows.length - 1);
        setFocusIndex(next);
        const element = document.querySelector<HTMLElement>(
          `[data-row-index="${next}"]`,
        );
        element?.focus();
      };
      if (event.key === "ArrowDown") move(1);
      else if (event.key === "ArrowUp") move(-1);
      else if (event.key === "ArrowRight" && collapsed.has(row.span.span_id))
        toggle(row.span.span_id);
      else if (
        event.key === "ArrowLeft" &&
        row.childCount > 0 &&
        !collapsed.has(row.span.span_id)
      )
        toggle(row.span.span_id);
      else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onSelect(row.span.span_id);
      }
    },
    [collapsed, onSelect, rows.length, toggle],
  );

  if (detail.spans.length === 0) {
    return (
      <p style={{ color: "var(--text-muted)" }}>This trace has no spans yet.</p>
    );
  }

  return (
    <div>
      {detail.orphan_span_ids.length > 0 && (
        <p
          role="note"
          style={{
            margin: "0 0 0.5rem",
            fontSize: "0.75rem",
            color: "var(--warn)",
          }}
        >
          ⚠ {detail.orphan_span_ids.length} span(s) reference a parent that has
          not arrived; they are shown at the top level.
        </p>
      )}

      <div
        role="tree"
        aria-label="Span waterfall"
        style={{ fontSize: "0.8125rem", fontFamily: "var(--mono)" }}
      >
        {rows.map((row, index) => {
          const { span } = row;
          const start = new Date(span.start_time).getTime();
          const duration = span.duration_ms ?? 0;
          const left = ((start - originNs) / spanNs) * 100;
          const width = Math.max((duration / spanNs) * 100, 0.4);
          const onPath = criticalPath.has(span.span_id);
          const isSelected = span.span_id === selectedSpanId;
          const isCollapsed = collapsed.has(span.span_id);

          return (
            <div
              key={span.span_id}
              role="treeitem"
              aria-level={row.depth + 1}
              aria-selected={isSelected}
              aria-expanded={row.childCount > 0 ? !isCollapsed : undefined}
              data-row-index={index}
              data-testid={`waterfall-row-${span.span_id}`}
              tabIndex={index === focusIndex ? 0 : -1}
              onClick={() => onSelect(span.span_id)}
              onKeyDown={(event) => onKeyDown(event, index, row)}
              style={{
                display: "grid",
                gridTemplateColumns:
                  "minmax(16rem, 34%) 1fr minmax(5rem, auto)",
                gap: "0.5rem",
                alignItems: "center",
                padding: "0.1875rem 0.5rem",
                background: isSelected ? "var(--accent-subtle)" : undefined,
                borderLeft: `2px solid ${onPath ? "var(--accent)" : "transparent"}`,
                cursor: "pointer",
              }}
            >
              <span
                style={{
                  paddingLeft: `${row.depth * 0.9}rem`,
                  display: "flex",
                  alignItems: "center",
                  gap: "0.25rem",
                  overflow: "hidden",
                }}
              >
                {row.childCount > 0 ? (
                  <button
                    type="button"
                    aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${span.name}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      toggle(span.span_id);
                    }}
                    style={{
                      border: "none",
                      background: "transparent",
                      color: "var(--text-muted)",
                      cursor: "pointer",
                      padding: 0,
                      width: "1rem",
                    }}
                  >
                    {isCollapsed ? "▸" : "▾"}
                  </button>
                ) : (
                  <span style={{ width: "1rem" }} aria-hidden="true" />
                )}
                <span
                  aria-hidden="true"
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: 2,
                    flexShrink: 0,
                    background:
                      CATEGORY_COLOUR[span.category] ?? "var(--text-faint)",
                  }}
                />
                <span
                  style={{
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={`${span.name} · ${span.category} · ${span.service_name}`}
                >
                  {span.name}
                </span>
                {isCollapsed && row.descendantCount > 0 && (
                  <span
                    style={{
                      color: "var(--text-faint)",
                      fontSize: "0.6875rem",
                    }}
                  >
                    (+{row.descendantCount})
                  </span>
                )}
                {span.status === "error" && (
                  <span
                    style={{ color: "var(--error)" }}
                    title="This span failed"
                  >
                    ✕
                  </span>
                )}
              </span>

              <span
                style={{
                  position: "relative",
                  height: 14,
                  background: "var(--bg-subtle)",
                  borderRadius: 3,
                }}
              >
                <span
                  style={{
                    position: "absolute",
                    left: `${left}%`,
                    width: `${width}%`,
                    top: 2,
                    bottom: 2,
                    minWidth: 2,
                    borderRadius: 2,
                    background:
                      span.status === "error"
                        ? "var(--error)"
                        : (CATEGORY_COLOUR[span.category] ??
                          "var(--text-faint)"),
                    opacity: onPath ? 1 : 0.65,
                  }}
                  title={`${formatDuration(span.duration_ms)}${
                    span.self_time_ms !== null
                      ? ` (self ${formatDuration(span.self_time_ms)})`
                      : ""
                  }`}
                />
              </span>

              <span
                style={{
                  textAlign: "right",
                  color: "var(--text-muted)",
                  whiteSpace: "nowrap",
                }}
              >
                {formatDuration(span.duration_ms)}
              </span>
            </div>
          );
        })}
      </div>

      <p
        style={{
          marginTop: "0.75rem",
          fontSize: "0.75rem",
          color: "var(--text-muted)",
        }}
      >
        <span
          aria-hidden="true"
          style={{
            display: "inline-block",
            width: 2,
            height: "0.8em",
            background: "var(--accent)",
            marginRight: "0.35rem",
            verticalAlign: "middle",
          }}
        />
        Bars on the critical path are drawn at full opacity with a left rule:
        they are the chain that determined total latency. Optimising anything
        else cannot make this request faster.
      </p>
    </div>
  );
}

/** Everything known about one span, shown beside the waterfall. */
export function SpanDetail({
  detail,
  spanId,
}: {
  detail: TraceDetail;
  spanId: string;
}) {
  const span = detail.spans.find((item) => item.span_id === spanId);
  if (!span) return null;

  const events = detail.events.filter((event) => event.span_id === spanId);
  const cost = detail.cost_records.find((record) => record.span_id === spanId);
  const onPath = detail.critical_path.includes(spanId);

  const attributes = Object.entries(span.attributes ?? {});

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div>
        <h3 style={{ margin: "0 0 0.25rem", fontSize: "0.9375rem" }}>
          {span.name}
        </h3>
        <div style={{ display: "flex", gap: "0.375rem", flexWrap: "wrap" }}>
          <StatusBadge status={span.status} />
          <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
            {span.category} · {span.kind} · {span.service_name}
          </span>
          {onPath && (
            <span style={{ fontSize: "0.75rem", color: "var(--accent)" }}>
              on critical path
            </span>
          )}
          {span.late_arrival && (
            <span style={{ fontSize: "0.75rem", color: "var(--warn)" }}>
              late arrival
            </span>
          )}
        </div>
      </div>

      <Section title="Timing">
        <Rows
          items={[
            ["Duration", formatDuration(span.duration_ms)],
            ["Self time", formatDuration(span.self_time_ms)],
            [
              "Time to first token",
              formatDuration(span.time_to_first_token_ms),
            ],
            ["Started", new Date(span.start_time).toISOString()],
          ]}
        />
      </Section>

      {(span.model || span.provider) && (
        <Section title="Model">
          <Rows
            items={[
              ["Provider", span.provider || "—"],
              ["Model", span.model || "—"],
            ]}
          />
        </Section>
      )}

      {(span.total_tokens !== null || span.input_tokens !== null) && (
        <Section title="Usage">
          <Rows
            items={[
              ["Input tokens", formatNumber(span.input_tokens)],
              ["Output tokens", formatNumber(span.output_tokens)],
              ["Cached input", formatNumber(span.cached_input_tokens)],
              ["Reasoning", formatNumber(span.reasoning_tokens)],
              ["Source", <StatusBadge key="s" status={span.usage_source} />],
            ]}
          />
        </Section>
      )}

      {cost && (
        <Section title="Cost">
          <Rows
            items={[
              ["Total", formatCost(cost.total, cost.currency)],
              [
                "Status",
                <StatusBadge key="c" status={cost.estimation_status} />,
              ],
              ["Price book", cost.price_book_version || "—"],
              [
                "Formula",
                <code
                  key="f"
                  style={{ fontSize: "0.6875rem", wordBreak: "break-all" }}
                >
                  {cost.formula}
                </code>,
              ],
            ]}
          />
        </Section>
      )}

      {(span.prompt_version_id ||
        span.model_config_id ||
        span.dataset_version_id) && (
        <Section title="Lineage">
          <Rows
            items={[
              ["Prompt", span.prompt_name || "—"],
              ["Prompt version", span.prompt_version_id || "—"],
              ["Model config", span.model_config_id || "—"],
              ["Dataset version", span.dataset_version_id || "—"],
              ["Knowledge base", span.knowledge_base_version || "—"],
            ]}
          />
        </Section>
      )}

      {span.status === "error" && (
        <Section title="Error">
          <Rows
            items={[
              ["Type", span.error_type || "—"],
              ["Message", span.error_message || span.status_message || "—"],
            ]}
          />
        </Section>
      )}

      {span.input_preview && (
        <Section title="Input">
          <pre style={preStyle}>{span.input_preview}</pre>
        </Section>
      )}
      {span.output_preview && (
        <Section title="Output">
          <pre style={preStyle}>{span.output_preview}</pre>
        </Section>
      )}

      {events.length > 0 && (
        <Section title={`Events (${events.length})`}>
          <ul style={{ margin: 0, paddingLeft: "1.1rem", fontSize: "0.75rem" }}>
            {events.map((event) => (
              <li key={`${event.span_id}-${event.sequence}`}>
                <strong>{event.name}</strong> ·{" "}
                {new Date(event.time).toISOString()}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {span.links.length > 0 && (
        <Section title={`Links (${span.links.length})`}>
          <ul style={{ margin: 0, paddingLeft: "1.1rem", fontSize: "0.75rem" }}>
            {span.links.map((link) => (
              <li key={link.span_id} style={{ fontFamily: "var(--mono)" }}>
                {link.trace_id.slice(0, 12)}…/{link.span_id.slice(0, 8)}…
              </li>
            ))}
          </ul>
        </Section>
      )}

      {attributes.length > 0 && (
        <Section title={`Attributes (${attributes.length})`}>
          <pre style={{ ...preStyle, maxHeight: "18rem" }}>
            {attributes
              .sort(([left], [right]) => left.localeCompare(right))
              .map(([key, value]) => `${key} = ${JSON.stringify(value)}`)
              .join("\n")}
          </pre>
        </Section>
      )}
    </div>
  );
}

const preStyle: React.CSSProperties = {
  margin: 0,
  padding: "0.5rem",
  background: "var(--bg-subtle)",
  borderRadius: "var(--radius)",
  fontFamily: "var(--mono)",
  fontSize: "0.75rem",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  maxHeight: "12rem",
  overflow: "auto",
};

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <h4
        style={{
          margin: "0 0 0.375rem",
          fontSize: "0.6875rem",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          color: "var(--text-muted)",
        }}
      >
        {title}
      </h4>
      {children}
    </div>
  );
}

function Rows({ items }: { items: [string, React.ReactNode][] }) {
  return (
    <dl
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(8rem, auto) 1fr",
        gap: "0.1875rem 0.75rem",
        margin: 0,
        fontSize: "0.75rem",
      }}
    >
      {items.map(([key, value]) => (
        <div key={key} style={{ display: "contents" }}>
          <dt style={{ color: "var(--text-muted)" }}>{key}</dt>
          <dd style={{ margin: 0, wordBreak: "break-word" }}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
