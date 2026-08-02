"use client";

/**
 * Latency dashboard.
 *
 * Percentiles, not averages. The mean latency of an AI request is dominated by
 * the fast cached path and hides exactly the tail that users complain about,
 * and averaging percentiles across groups is arithmetically meaningless — so
 * every percentile here is computed by the store over the raw rows.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { useWorkspace } from "../providers";
import { api } from "@/lib/api";
import { TimeSeriesChart, seriesFromDashboard } from "@/components/Charts";
import {
  Card,
  ErrorState,
  Loading,
  Select,
  Column,
  DataTable,
} from "@/components/ui";
import { formatDuration, formatNumber, timeWindow } from "@/lib/format";

const GROUPINGS = [
  { value: "model", label: "Model" },
  { value: "provider", label: "Provider" },
  { value: "service_name", label: "Service" },
  { value: "category", label: "Span category" },
  { value: "name", label: "Span name" },
];

const PERCENTILES = [
  { value: "p50", label: "p50 (median)" },
  { value: "p90", label: "p90" },
  { value: "p95", label: "p95" },
  { value: "p99", label: "p99" },
  { value: "max", label: "max" },
];

interface Row {
  keys: string[];
  count: number;
  p50: number | null;
  p90: number | null;
  p95: number | null;
  p99: number | null;
  avg: number | null;
  max: number | null;
}

export default function LatencyPage() {
  const workspace = useWorkspace();
  const [groupBy, setGroupBy] = useState("model");
  const [percentile, setPercentile] = useState("p95");
  const [source, setSource] = useState("spans");
  const window = useMemo(() => timeWindow(workspace.range), [workspace.range]);

  const breakdown = useQuery({
    queryKey: [
      "latency",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
      groupBy,
      source,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.latency({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        group_by: [groupBy],
        source,
      }),
  });

  const series = useQuery({
    queryKey: [
      "latency-series",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
      groupBy,
      percentile,
      source,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.timeseries({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        metric: "duration_ms",
        aggregation: percentile,
        group_by: [groupBy],
        source,
      }),
  });

  const ttft = useQuery({
    queryKey: [
      "ttft-series",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
      percentile,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.timeseries({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        metric: "time_to_first_token_ms",
        aggregation: percentile,
        source: "spans",
      }),
  });

  const columns: Column<Row>[] = [
    {
      key: "group",
      header: GROUPINGS.find((g) => g.value === groupBy)?.label ?? "Group",
      render: (row) => row.keys.join(" / ") || "(none)",
    },
    {
      key: "count",
      header: "Samples",
      align: "right",
      render: (row) => formatNumber(row.count),
    },
    {
      key: "p50",
      header: "p50",
      align: "right",
      render: (row) => formatDuration(row.p50),
    },
    {
      key: "p90",
      header: "p90",
      align: "right",
      render: (row) => formatDuration(row.p90),
    },
    {
      key: "p95",
      header: "p95",
      align: "right",
      render: (row) => formatDuration(row.p95),
    },
    {
      key: "p99",
      header: "p99",
      align: "right",
      render: (row) => formatDuration(row.p99),
    },
    {
      key: "max",
      header: "max",
      align: "right",
      render: (row) => formatDuration(row.max),
    },
    {
      key: "spread",
      header: "p99 / p50",
      align: "right",
      render: (row) =>
        row.p50 && row.p99 ? (
          <span title="A high ratio means a long tail: most requests are fine and a few are not.">
            {(row.p99 / row.p50).toFixed(1)}×
          </span>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Latency</h1>
          <p>
            Percentiles computed over raw spans. Averages are shown only for
            reference — they hide the tail that users actually notice.
          </p>
        </div>
        <div style={{ display: "flex", gap: "0.75rem" }}>
          <Select
            label="Group by"
            value={groupBy}
            onChange={setGroupBy}
            options={GROUPINGS}
          />
          <Select
            label="Percentile"
            value={percentile}
            onChange={setPercentile}
            options={PERCENTILES}
          />
          <Select
            label="Source"
            value={source}
            onChange={setSource}
            options={[
              { value: "spans", label: "Spans" },
              { value: "traces", label: "Whole traces" },
            ]}
          />
        </div>
      </header>

      <Card
        title={`${percentile} duration over time`}
        subtitle={`grouped by ${groupBy}`}
      >
        {series.isLoading && <Loading />}
        {series.isError && (
          <ErrorState error={series.error} onRetry={() => series.refetch()} />
        )}
        {series.data && (
          <TimeSeriesChart
            series={seriesFromDashboard(series.data)}
            unit={series.data.unit}
            partialBuckets={series.data.partial_buckets}
            valueFormat="ms"
            height={260}
          />
        )}
      </Card>

      <div style={{ marginTop: "1rem" }}>
        <Card
          title={`Time to first token (${percentile})`}
          subtitle="what a user waits before any output appears; only streamed responses record it"
        >
          {ttft.isLoading && <Loading />}
          {ttft.isError && (
            <ErrorState error={ttft.error} onRetry={() => ttft.refetch()} />
          )}
          {ttft.data && (
            <TimeSeriesChart
              series={seriesFromDashboard(ttft.data)}
              unit={ttft.data.unit}
              partialBuckets={ttft.data.partial_buckets}
              valueFormat="ms"
            />
          )}
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}>
        <Card title="Breakdown" padded={false}>
          {breakdown.isLoading && <Loading />}
          {breakdown.isError && (
            <ErrorState
              error={breakdown.error}
              onRetry={() => breakdown.refetch()}
            />
          )}
          {breakdown.data && (
            <DataTable
              columns={columns}
              rows={breakdown.data.groups as Row[]}
              rowKey={(row) => row.keys.join("|") || "none"}
              caption={`Latency percentiles grouped by ${groupBy}`}
              emptyMessage="No spans in this window"
            />
          )}
        </Card>
      </div>
    </>
  );
}
