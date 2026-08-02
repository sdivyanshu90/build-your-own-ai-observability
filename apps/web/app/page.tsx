"use client";

/**
 * Overview dashboard.
 *
 * Answers the four questions someone opens an observability tool with, in the
 * order they ask them: is it up, is it slow, is it expensive, and what broke.
 * Every number is compared against the previous window of the same length, so
 * "300ms p95" is reported as a change rather than as a bare fact.
 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { useWorkspace } from "./providers";
import { api } from "@/lib/api";
import { TimeSeriesChart, seriesFromDashboard } from "@/components/Charts";
import {
  Card,
  ErrorState,
  Grid,
  Loading,
  PartialDataNotice,
  Stat,
  StatusBadge,
} from "@/components/ui";
import {
  formatCost,
  formatDuration,
  formatNumber,
  formatPercent,
  formatRelative,
  timeWindow,
} from "@/lib/format";

export default function OverviewPage() {
  const workspace = useWorkspace();
  const { projectId, environment, range } = workspace;
  const window = useMemo(() => timeWindow(range), [range]);

  const overview = useQuery({
    queryKey: ["overview", projectId, environment, window.start, window.end],
    enabled: Boolean(projectId),
    queryFn: () =>
      api.overview({
        project_id: projectId!,
        environment,
        start: window.start,
        end: window.end,
        compare_previous: true,
      }),
  });

  const requests = useQuery({
    queryKey: [
      "timeseries",
      "requests",
      projectId,
      environment,
      window.start,
      window.end,
    ],
    enabled: Boolean(projectId),
    queryFn: () =>
      api.timeseries({
        project_id: projectId!,
        environment,
        start: window.start,
        end: window.end,
        aggregation: "count",
        source: "traces",
      }),
  });

  const latency = useQuery({
    queryKey: [
      "timeseries",
      "latency",
      projectId,
      environment,
      window.start,
      window.end,
    ],
    enabled: Boolean(projectId),
    queryFn: () =>
      api.timeseries({
        project_id: projectId!,
        environment,
        start: window.start,
        end: window.end,
        metric: "duration_ms",
        aggregation: "p95",
        source: "traces",
      }),
  });

  const recentErrors = useQuery({
    queryKey: [
      "traces",
      "errors",
      projectId,
      environment,
      window.start,
      window.end,
    ],
    enabled: Boolean(projectId),
    queryFn: () =>
      api.traces({
        project_id: projectId!,
        environment,
        start: window.start,
        end: window.end,
        filter: ["status:eq:error"],
        sort: "-start_time",
        limit: 8,
      }),
  });

  if (workspace.loading) return <Loading label="Loading workspace" />;
  if (workspace.error)
    return <ErrorState error={workspace.error} onRetry={workspace.reload} />;
  if (!projectId) {
    return (
      <ErrorState
        error={
          new Error(
            "No project is available for this account. Ask an administrator to grant access.",
          )
        }
      />
    );
  }

  const summary = overview.data;
  const previous = summary?.previous ?? null;

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Overview</h1>
          <p>
            {workspace.project?.name} · {environment} · last {range}
          </p>
        </div>
        <Link href="/traces">Explore traces →</Link>
      </header>

      {overview.isLoading && <Loading label="Loading summary" />}
      {overview.isError && (
        <ErrorState error={overview.error} onRetry={() => overview.refetch()} />
      )}

      {summary && (
        <>
          {summary.cost_is_partial && (
            <PartialDataNotice reason="Some spans in this window have no price for their model, so the cost total is a lower bound. Unpriced usage is not counted as zero — see the cost dashboard for which models are affected." />
          )}

          <Grid columns={5}>
            <Stat
              label="Requests"
              value={formatNumber(summary.request_count)}
              previous={
                previous ? formatNumber(previous.request_count) : undefined
              }
              delta={delta(summary.request_count, previous?.request_count)}
            />
            <Stat
              label="Error rate"
              value={formatPercent(summary.error_rate, 2)}
              previous={
                previous ? formatPercent(previous.error_rate, 2) : undefined
              }
              delta={delta(summary.error_rate, previous?.error_rate)}
              invertDelta
              detail={`${formatNumber(summary.error_count)} failed`}
            />
            <Stat
              label="p95 latency"
              value={formatDuration(summary.latency?.p95 ?? null)}
              previous={
                previous
                  ? formatDuration(previous.latency?.p95 ?? null)
                  : undefined
              }
              delta={delta(
                summary.latency?.p95 ?? null,
                previous?.latency?.p95 ?? null,
              )}
              invertDelta
              detail={`p50 ${formatDuration(summary.latency?.p50 ?? null)} · p99 ${formatDuration(
                summary.latency?.p99 ?? null,
              )}`}
            />
            <Stat
              label="Tokens"
              value={formatNumber(summary.total_tokens)}
              detail={`${formatNumber(summary.input_tokens)} in · ${formatNumber(summary.output_tokens)} out`}
              delta={delta(summary.total_tokens, previous?.total_tokens)}
            />
            <Stat
              label="Cost"
              value={formatCost(summary.total_cost, summary.cost_currency)}
              detail={summary.cost_is_partial ? "lower bound" : "complete"}
              previous={
                previous
                  ? formatCost(previous.total_cost, previous.cost_currency)
                  : undefined
              }
              invertDelta
            />
          </Grid>

          {summary.time_to_first_token &&
            summary.time_to_first_token.count > 0 && (
              <div style={{ marginTop: "0.75rem" }}>
                <Grid columns={4}>
                  <Stat
                    label="Time to first token p50"
                    value={formatDuration(summary.time_to_first_token.p50)}
                    detail={`${formatNumber(summary.time_to_first_token.count)} streamed responses`}
                  />
                  <Stat
                    label="Time to first token p95"
                    value={formatDuration(summary.time_to_first_token.p95)}
                    detail="what a user actually waits before seeing anything"
                  />
                </Grid>
              </div>
            )}
        </>
      )}

      <div
        style={{
          display: "grid",
          gap: "1rem",
          gridTemplateColumns: "repeat(auto-fit, minmax(24rem, 1fr))",
          marginTop: "1rem",
        }}
      >
        <Card title="Requests" subtitle="count per bucket">
          {requests.isLoading && <Loading />}
          {requests.isError && (
            <ErrorState
              error={requests.error}
              onRetry={() => requests.refetch()}
            />
          )}
          {requests.data && (
            <TimeSeriesChart
              series={seriesFromDashboard(requests.data)}
              unit={requests.data.unit}
              partialBuckets={requests.data.partial_buckets}
            />
          )}
        </Card>

        <Card title="Latency" subtitle="p95, milliseconds">
          {latency.isLoading && <Loading />}
          {latency.isError && (
            <ErrorState
              error={latency.error}
              onRetry={() => latency.refetch()}
            />
          )}
          {latency.data && (
            <TimeSeriesChart
              series={seriesFromDashboard(latency.data)}
              unit={latency.data.unit}
              partialBuckets={latency.data.partial_buckets}
              valueFormat="ms"
            />
          )}
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}>
        <Card
          title="Recent failures"
          subtitle="most recent errored traces in this window"
        >
          {recentErrors.isLoading && <Loading />}
          {recentErrors.isError && (
            <ErrorState
              error={recentErrors.error}
              onRetry={() => recentErrors.refetch()}
            />
          )}
          {recentErrors.data && recentErrors.data.items.length === 0 && (
            <p style={{ margin: 0, color: "var(--text-muted)" }}>
              <span aria-hidden="true">✓ </span>No errored traces in the last{" "}
              {range}.
            </p>
          )}
          {recentErrors.data && recentErrors.data.items.length > 0 && (
            <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
              {recentErrors.data.items.map((trace) => (
                <li
                  key={trace.trace_id}
                  style={{
                    display: "flex",
                    gap: "0.75rem",
                    alignItems: "center",
                    padding: "0.375rem 0",
                    borderBottom: "1px solid var(--border)",
                  }}
                >
                  <StatusBadge status="error" />
                  <Link
                    href={`/traces/${trace.trace_id}`}
                    style={{ fontFamily: "var(--mono)", fontSize: "0.75rem" }}
                  >
                    {trace.trace_id.slice(0, 16)}…
                  </Link>
                  <span
                    style={{
                      flex: 1,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {trace.name || "unnamed"}
                  </span>
                  <span
                    style={{ color: "var(--text-muted)", fontSize: "0.75rem" }}
                  >
                    {formatDuration(trace.duration_ms)}
                  </span>
                  <span
                    style={{ color: "var(--text-faint)", fontSize: "0.75rem" }}
                  >
                    {formatRelative(trace.start_time)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}

function delta(
  current: number | null | undefined,
  previous: number | null | undefined,
) {
  if (
    current === null ||
    current === undefined ||
    previous === null ||
    previous === undefined
  ) {
    return undefined;
  }
  const absolute = current - previous;
  const relative = previous === 0 ? null : absolute / previous;
  return { absolute, relative };
}
