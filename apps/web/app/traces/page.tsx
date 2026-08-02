"use client";

/**
 * Trace explorer.
 *
 * Filters live in the URL, not in component state. A debugging session that
 * cannot be pasted into an incident channel is worth much less than one that
 * can, and "reload lost my filters" is a reliable way to make people stop using
 * a tool.
 *
 * Paging is cursor-based. Offset paging over an append-only, time-ordered store
 * silently duplicates and skips rows while new data lands mid-session, which is
 * exactly when someone is looking.
 */

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Trace } from "@aiobs/schemas";

import { useWorkspace } from "../providers";
import { api } from "@/lib/api";
import {
  Button,
  Card,
  Column,
  DataTable,
  ErrorState,
  Loading,
  Select,
  StatusBadge,
  Tag,
  TextInput,
} from "@/components/ui";
import {
  formatCost,
  formatDuration,
  formatNumber,
  formatRelative,
  timeWindow,
} from "@/lib/format";

// The API's sort grammar is a comma-separated field list where a leading `-`
// means descending. Keeping these values in exactly that form means the URL
// carries the server's own vocabulary rather than a translation of it.
const SORTS = [
  { value: "-start_time", label: "Newest first" },
  { value: "start_time", label: "Oldest first" },
  { value: "-duration_ms", label: "Slowest first" },
  { value: "-cost", label: "Most expensive first" },
  { value: "-total_tokens", label: "Most tokens first" },
];

const QUICK_FILTERS = [
  { value: "", label: "All traces" },
  { value: "status:eq:error", label: "Errors only" },
  { value: "cost_status:eq:unpriced", label: "Unpriced usage" },
  { value: "complete:eq:false", label: "Incomplete traces" },
  { value: "usage_source:eq:estimated", label: "Estimated tokens" },
];

export default function TracesPage() {
  return (
    <Suspense fallback={<Loading label="Loading traces" />}>
      <TraceExplorer />
    </Suspense>
  );
}

function TraceExplorer() {
  const workspace = useWorkspace();
  const router = useRouter();
  const params = useSearchParams();

  const sort = params.get("sort") ?? "-start_time";
  const quick = params.get("quick") ?? "";
  const custom = params.get("filter") ?? "";
  const search = params.get("q") ?? "";
  const [searchDraft, setSearchDraft] = useState(search);
  const [customDraft, setCustomDraft] = useState(custom);
  const [cursors, setCursors] = useState<string[]>([]);

  const window = useMemo(() => timeWindow(workspace.range), [workspace.range]);
  const filters = useMemo(
    () => [quick, custom].filter((value): value is string => Boolean(value)),
    [quick, custom],
  );

  const setParam = useCallback(
    (updates: Record<string, string | null>) => {
      const next = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(updates)) {
        if (value === null || value === "") next.delete(key);
        else next.set(key, value);
      }
      setCursors([]);
      router.replace(`/traces?${next.toString()}`);
    },
    [params, router],
  );

  const cursor = cursors[cursors.length - 1];
  const query = useQuery({
    queryKey: [
      "traces",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
      filters,
      sort,
      search,
      cursor ?? null,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.traces({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        filter: filters,
        sort,
        q: search || undefined,
        limit: 50,
        cursor,
      }),
  });

  const columns: Column<Trace>[] = [
    {
      key: "status",
      header: "Status",
      width: "6.5rem",
      render: (trace) => <StatusBadge status={trace.status} />,
    },
    {
      key: "name",
      header: "Name",
      render: (trace) => (
        <Link href={`/traces/${trace.trace_id}`} style={{ fontWeight: 500 }}>
          {trace.name || "unnamed"}
        </Link>
      ),
    },
    {
      key: "trace_id",
      header: "Trace",
      render: (trace) => (
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: "0.75rem",
            color: "var(--text-muted)",
          }}
        >
          {trace.trace_id.slice(0, 12)}…
        </span>
      ),
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      render: (trace) => (
        <>
          {formatDuration(trace.duration_ms)}
          {!trace.complete && (
            <span
              title="Trace is still open or lost spans"
              style={{ color: "var(--warn)" }}
            >
              {" "}
              ⚠
            </span>
          )}
        </>
      ),
    },
    {
      key: "ttft",
      header: "TTFT",
      align: "right",
      render: (trace) => formatDuration(trace.time_to_first_token_ms),
    },
    {
      key: "tokens",
      header: "Tokens",
      align: "right",
      render: (trace) => (
        <span
          title={`${trace.input_tokens} in / ${trace.output_tokens} out (${trace.usage_source})`}
        >
          {formatNumber(trace.total_tokens)}
          {trace.usage_source !== "provider" && (
            <span style={{ color: "var(--text-faint)" }}>
              {" "}
              {trace.usage_source[0]}
            </span>
          )}
        </span>
      ),
    },
    {
      key: "cost",
      header: "Cost",
      align: "right",
      render: (trace) => (
        <span title={`estimation: ${trace.cost_status}`}>
          {formatCost(trace.cost, trace.cost_currency)}
          {trace.cost_status === "unpriced" && (
            <span
              style={{ color: "var(--warn)" }}
              title="No price book entry covers this model"
            >
              {" "}
              ?
            </span>
          )}
        </span>
      ),
    },
    {
      key: "spans",
      header: "Spans",
      align: "right",
      render: (trace) => formatNumber(trace.span_count),
    },
    {
      key: "models",
      header: "Models",
      render: (trace) => (
        <span style={{ display: "flex", gap: "0.25rem", flexWrap: "wrap" }}>
          {trace.models.slice(0, 2).map((model) => (
            <Tag key={model}>{model}</Tag>
          ))}
          {trace.models.length > 2 && <Tag>+{trace.models.length - 2}</Tag>}
        </span>
      ),
    },
    {
      key: "when",
      header: "When",
      align: "right",
      render: (trace) => (
        <span title={trace.start_time} style={{ color: "var(--text-muted)" }}>
          {formatRelative(trace.start_time)}
        </span>
      ),
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Traces</h1>
          <p>
            One row per request. Filters and sort are stored in the URL, so this
            view can be shared verbatim.
          </p>
        </div>
        <Link href="/traces/compare">Compare two traces →</Link>
      </header>

      <Card title="Filters">
        <div
          style={{
            display: "flex",
            gap: "0.75rem",
            flexWrap: "wrap",
            alignItems: "flex-end",
          }}
        >
          <Select
            label="Quick filter"
            value={quick}
            onChange={(value) => setParam({ quick: value })}
            options={QUICK_FILTERS}
          />
          <Select
            label="Sort"
            value={sort}
            onChange={(value) => setParam({ sort: value })}
            options={SORTS}
          />
          <form
            style={{
              display: "flex",
              gap: "0.5rem",
              alignItems: "flex-end",
              flex: "1 1 16rem",
            }}
            onSubmit={(event) => {
              event.preventDefault();
              setParam({ q: searchDraft });
            }}
          >
            <TextInput
              label="Search name / session / subject"
              value={searchDraft}
              onChange={setSearchDraft}
              placeholder="checkout-assistant"
            />
            <Button type="submit">Search</Button>
          </form>
          <form
            style={{
              display: "flex",
              gap: "0.5rem",
              alignItems: "flex-end",
              flex: "1 1 18rem",
            }}
            onSubmit={(event) => {
              event.preventDefault();
              setParam({ filter: customDraft });
            }}
          >
            <TextInput
              label="Expression filter (field:op:value)"
              value={customDraft}
              onChange={setCustomDraft}
              placeholder="model:contains:gpt-4"
              describedBy="filter-help"
            />
            <Button type="submit">Apply</Button>
          </form>
        </div>
        <p
          id="filter-help"
          style={{
            margin: "0.5rem 0 0",
            fontSize: "0.75rem",
            color: "var(--text-muted)",
          }}
        >
          The grammar is closed: only declared fields and operators are
          accepted, and the server rejects anything else rather than guessing.
          Operators include{" "}
          <code>
            eq, ne, gt, gte, lt, lte, contains, starts_with, in, exists
          </code>
          .
        </p>
      </Card>

      <div style={{ marginTop: "1rem" }}>
        <Card
          title="Results"
          subtitle={
            query.data
              ? `${query.data.items.length} trace(s)${query.data.has_more ? ", more available" : ""}`
              : undefined
          }
          padded={false}
        >
          {query.isLoading && <Loading label="Querying traces" />}
          {query.isError && (
            <ErrorState error={query.error} onRetry={() => query.refetch()} />
          )}
          {query.data && (
            <>
              <DataTable
                columns={columns}
                rows={query.data.items}
                rowKey={(trace) => trace.trace_id}
                caption="Traces matching the current filters"
                emptyMessage="No traces match these filters in this time range"
              />
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  padding: "0.75rem 1rem",
                  borderTop: "1px solid var(--border)",
                }}
              >
                <Button
                  disabled={cursors.length === 0}
                  onClick={() => setCursors((stack) => stack.slice(0, -1))}
                >
                  ← Previous
                </Button>
                <span
                  style={{
                    color: "var(--text-muted)",
                    fontSize: "0.75rem",
                    alignSelf: "center",
                  }}
                >
                  Page {cursors.length + 1}
                </span>
                <Button
                  disabled={!query.data.has_more || !query.data.next_cursor}
                  onClick={() =>
                    setCursors((stack) =>
                      query.data?.next_cursor
                        ? [...stack, query.data.next_cursor]
                        : stack,
                    )
                  }
                >
                  Next →
                </Button>
              </div>
            </>
          )}
        </Card>
      </div>
    </>
  );
}
