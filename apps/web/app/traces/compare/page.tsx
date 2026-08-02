"use client";

/**
 * Trace comparison.
 *
 * The question this answers is "what changed between the good run and the bad
 * one", so the layout is deltas first and detail second. Spans are matched by
 * structural position rather than by span id -- ids differ between runs, and a
 * comparison keyed on them would report every span as new.
 */

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { useWorkspace } from "../../providers";
import { api } from "@/lib/api";
import {
  Button,
  Card,
  ErrorState,
  KeyValue,
  Loading,
  StatusBadge,
  TextInput,
} from "@/components/ui";
import {
  formatCost,
  formatDuration,
  formatNumber,
  formatTimestamp,
} from "@/lib/format";

export default function ComparePage() {
  return (
    <Suspense fallback={<Loading label="Loading comparison" />}>
      <CompareView />
    </Suspense>
  );
}

function CompareView() {
  const workspace = useWorkspace();
  const router = useRouter();
  const params = useSearchParams();

  const left = params.get("left") ?? "";
  const right = params.get("right") ?? "";
  const [leftDraft, setLeftDraft] = useState(left);
  const [rightDraft, setRightDraft] = useState(right);

  const comparison = useQuery({
    queryKey: ["compare", workspace.projectId, left, right],
    enabled: Boolean(workspace.projectId && left && right),
    queryFn: () =>
      api.compare({ project_id: workspace.projectId!, left, right }),
  });

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Compare traces</h1>
          <p>
            Side-by-side deltas for two runs of the same workflow. Spans are
            matched by name and position, so ids differing between runs does not
            matter.
          </p>
        </div>
        <Link href="/traces">← Back to traces</Link>
      </header>

      <Card title="Select traces">
        <form
          style={{
            display: "flex",
            gap: "0.75rem",
            alignItems: "flex-end",
            flexWrap: "wrap",
          }}
          onSubmit={(event) => {
            event.preventDefault();
            router.replace(
              `/traces/compare?left=${encodeURIComponent(leftDraft)}&right=${encodeURIComponent(rightDraft)}`,
            );
          }}
        >
          <TextInput
            label="Baseline trace id"
            value={leftDraft}
            onChange={setLeftDraft}
          />
          <TextInput
            label="Candidate trace id"
            value={rightDraft}
            onChange={setRightDraft}
          />
          <Button
            type="submit"
            variant="primary"
            disabled={!leftDraft || !rightDraft}
          >
            Compare
          </Button>
        </form>
      </Card>

      {!left || !right ? null : (
        <div style={{ marginTop: "1rem" }}>
          {comparison.isLoading && <Loading label="Comparing" />}
          {comparison.isError && (
            <ErrorState
              error={comparison.error}
              onRetry={() => comparison.refetch()}
            />
          )}
          {comparison.data && <Result data={comparison.data} />}
        </div>
      )}
    </>
  );
}

type Comparison = Awaited<ReturnType<typeof api.compare>>;

function Result({ data }: { data: Comparison }) {
  const { left, right, summary_deltas: deltas, matched_spans: matched } = data;

  return (
    <div style={{ display: "grid", gap: "1rem" }}>
      <div
        style={{
          display: "grid",
          gap: "1rem",
          gridTemplateColumns: "repeat(auto-fit, minmax(20rem, 1fr))",
        }}
      >
        <Card title="Baseline" subtitle={left.trace_id}>
          <TraceSummary trace={left} />
        </Card>
        <Card title="Candidate" subtitle={right.trace_id}>
          <TraceSummary trace={right} />
        </Card>
      </div>

      <Card title="Deltas" subtitle="candidate relative to baseline">
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontSize: "0.8125rem",
          }}
        >
          <caption className="sr-only">
            Metric differences between the two traces
          </caption>
          <thead>
            <tr>
              {["Metric", "Baseline", "Candidate", "Change"].map((header) => (
                <th
                  key={header}
                  scope="col"
                  style={{
                    textAlign: header === "Metric" ? "left" : "right",
                    padding: "0.375rem 0.5rem",
                    borderBottom: "1px solid var(--border)",
                    color: "var(--text-muted)",
                    fontSize: "0.75rem",
                  }}
                >
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Object.entries(deltas).map(([metric, value]) => {
              // Cost is the one metric whose values are decimal strings. It is
              // formatted as money and never parsed into a float.
              const money = metric === "cost";
              return (
                <tr
                  key={metric}
                  style={{ borderBottom: "1px solid var(--border)" }}
                >
                  <th
                    scope="row"
                    style={{
                      textAlign: "left",
                      padding: "0.375rem 0.5rem",
                      fontWeight: 500,
                    }}
                  >
                    {metric.replace(/_/g, " ")}
                  </th>
                  <td
                    style={{ textAlign: "right", padding: "0.375rem 0.5rem" }}
                  >
                    {money
                      ? formatCost(asString(value.left))
                      : formatNumber(toNumber(value.left))}
                  </td>
                  <td
                    style={{ textAlign: "right", padding: "0.375rem 0.5rem" }}
                  >
                    {money
                      ? formatCost(asString(value.right))
                      : formatNumber(toNumber(value.right))}
                  </td>
                  <td
                    style={{ textAlign: "right", padding: "0.375rem 0.5rem" }}
                  >
                    {money ? (
                      <MoneyChange
                        absolute={asString(value.absolute)}
                        relative={value.relative}
                      />
                    ) : (
                      <Change
                        absolute={toNumber(value.absolute)}
                        relative={value.relative}
                      />
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      <Card
        title="Lineage differences"
        subtitle="the versions that differ are the first place to look for a regression"
      >
        {Object.entries(data.lineage_differences).length === 0 ? (
          <p style={{ margin: 0, color: "var(--text-muted)" }}>
            No lineage recorded on either trace.
          </p>
        ) : (
          <KeyValue
            items={Object.entries(data.lineage_differences).map(
              ([field, value]) => [
                field.replace(/_/g, " "),
                value.changed ? (
                  <span key={field} style={{ color: "var(--warn)" }}>
                    <span aria-hidden="true">≠ </span>
                    {describeLineage(value.left)} →{" "}
                    {describeLineage(value.right)}
                  </span>
                ) : (
                  <span key={field} style={{ color: "var(--text-muted)" }}>
                    identical ({describeLineage(value.left)})
                  </span>
                ),
              ],
            )}
          />
        )}
      </Card>

      <div
        style={{
          display: "grid",
          gap: "1rem",
          gridTemplateColumns: "repeat(auto-fit, minmax(18rem, 1fr))",
        }}
      >
        <Card
          title="Only in baseline"
          subtitle={`${data.only_in_left.length} span(s)`}
        >
          <SpanList
            names={data.only_in_left}
            empty="Nothing unique to the baseline."
          />
        </Card>
        <Card
          title="Only in candidate"
          subtitle={`${data.only_in_right.length} span(s)`}
        >
          <SpanList
            names={data.only_in_right}
            empty="Nothing unique to the candidate."
          />
        </Card>
      </div>

      <Card
        title="Matched spans"
        subtitle={`${matched.length} span(s) present in both traces`}
      >
        {matched.length === 0 ? (
          <p style={{ margin: 0, color: "var(--text-muted)" }}>
            No spans matched between the traces.
          </p>
        ) : (
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "0.8125rem",
            }}
          >
            <caption className="sr-only">Per-span duration comparison</caption>
            <thead>
              <tr>
                {["Span", "Baseline", "Candidate", "Change"].map((header) => (
                  <th
                    key={header}
                    scope="col"
                    style={{
                      textAlign: header === "Span" ? "left" : "right",
                      padding: "0.375rem 0.5rem",
                      borderBottom: "1px solid var(--border)",
                      color: "var(--text-muted)",
                      fontSize: "0.75rem",
                    }}
                  >
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matched.map((row, index) => {
                const name = String(
                  row.name ?? row.span_name ?? `span ${index}`,
                );
                const leftMs = toNumber(row.left_duration_ms);
                const rightMs = toNumber(row.right_duration_ms);
                const absolute =
                  leftMs !== null && rightMs !== null ? rightMs - leftMs : null;
                return (
                  <tr
                    key={`${name}-${index}`}
                    style={{ borderBottom: "1px solid var(--border)" }}
                  >
                    <th
                      scope="row"
                      style={{
                        textAlign: "left",
                        padding: "0.375rem 0.5rem",
                        fontWeight: 500,
                      }}
                    >
                      {name}
                    </th>
                    <td
                      style={{ textAlign: "right", padding: "0.375rem 0.5rem" }}
                    >
                      {formatDuration(leftMs)}
                    </td>
                    <td
                      style={{ textAlign: "right", padding: "0.375rem 0.5rem" }}
                    >
                      {formatDuration(rightMs)}
                    </td>
                    <td
                      style={{ textAlign: "right", padding: "0.375rem 0.5rem" }}
                    >
                      <Change
                        absolute={absolute}
                        relative={leftMs ? (absolute ?? 0) / leftMs : null}
                        unit="ms"
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

function TraceSummary({ trace }: { trace: Comparison["left"] }) {
  return (
    <KeyValue
      items={[
        ["Status", <StatusBadge key="s" status={trace.status} />],
        ["Name", trace.name || "unnamed"],
        ["Started", formatTimestamp(trace.start_time)],
        ["Duration", formatDuration(trace.duration_ms)],
        ["Spans", formatNumber(trace.span_count)],
        ["Tokens", formatNumber(trace.total_tokens)],
        ["Cost", formatCost(trace.cost, trace.cost_currency)],
        ["Models", trace.models.join(", ") || "—"],
        [
          "Open",
          <Link key="l" href={`/traces/${trace.trace_id}`}>
            view trace
          </Link>,
        ],
      ]}
    />
  );
}

function SpanList({ names, empty }: { names: string[]; empty: string }) {
  if (names.length === 0)
    return <p style={{ margin: 0, color: "var(--text-muted)" }}>{empty}</p>;
  return (
    <ul style={{ margin: 0, paddingLeft: "1rem", fontSize: "0.8125rem" }}>
      {names.map((name) => (
        <li key={name}>{name}</li>
      ))}
    </ul>
  );
}

/** A change, stated in words as well as with a sign. Whether a change is good
 *  depends on the metric, so this component deliberately does not colour it. */
function Change({
  absolute,
  relative,
  unit = "",
}: {
  absolute: number | null;
  relative: number | null;
  unit?: string;
}) {
  if (absolute === null)
    return <span style={{ color: "var(--text-faint)" }}>—</span>;
  if (absolute === 0)
    return <span style={{ color: "var(--text-muted)" }}>unchanged</span>;
  const sign = absolute > 0 ? "+" : "";
  return (
    <span>
      {sign}
      {absolute.toFixed(Math.abs(absolute) < 1 ? 4 : 1)}
      {unit}
      {relative !== null && Number.isFinite(relative) && (
        <span style={{ color: "var(--text-muted)" }}>
          {" "}
          ({sign}
          {(relative * 100).toFixed(1)}%)
        </span>
      )}
    </span>
  );
}

/** Money change rendered from the original decimal string. */
function MoneyChange({
  absolute,
  relative,
}: {
  absolute: string | null;
  relative: number | null;
}) {
  if (absolute === null)
    return <span style={{ color: "var(--text-faint)" }}>—</span>;
  const negative = absolute.startsWith("-");
  if (/^-?0*\.?0*$/.test(absolute))
    return <span style={{ color: "var(--text-muted)" }}>unchanged</span>;
  return (
    <span>
      {negative ? "-" : "+"}
      {formatCost(negative ? absolute.slice(1) : absolute)}
      {relative !== null && Number.isFinite(relative) && (
        <span style={{ color: "var(--text-muted)" }}>
          {" "}
          ({(relative * 100).toFixed(1)}%)
        </span>
      )}
    </span>
  );
}

/** Lineage values are either a list of version ids or a single string. */
function describeLineage(value: string[] | string | null | undefined): string {
  if (Array.isArray(value)) return value.join(", ") || "none";
  return value ? String(value) : "none";
}

function asString(value: unknown): string | null {
  return typeof value === "string"
    ? value
    : value === null || value === undefined
      ? null
      : String(value);
}

function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}
