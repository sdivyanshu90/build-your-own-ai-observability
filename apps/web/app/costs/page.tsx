"use client";

/**
 * Cost dashboard.
 *
 * Three rules this page exists to enforce:
 *
 * - **Unpriced is not free.** Usage the price book does not cover is reported
 *   separately, never folded into the total as zero. A total that silently
 *   omits a new model is worse than no total.
 * - **Money stays a decimal string** end to end. Nothing here parses a cost
 *   into a float except to size a bar.
 * - **Currencies are not summed.** If two price books disagree on currency the
 *   API returns them separately and so does this page.
 */

import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { useWorkspace } from "../providers";
import { api } from "@/lib/api";
import {
  BarChart,
  TimeSeriesChart,
  seriesFromDashboard,
} from "@/components/Charts";
import {
  Card,
  Column,
  DataTable,
  ErrorState,
  Loading,
  PartialDataNotice,
  Select,
  Stat,
} from "@/components/ui";
import {
  formatCost,
  formatNumber,
  formatPercent,
  timeWindow,
} from "@/lib/format";

const GROUPINGS = [
  { value: "model", label: "Model" },
  { value: "provider", label: "Provider" },
  { value: "service_name", label: "Service" },
  { value: "category", label: "Operation type" },
  { value: "prompt_version_id", label: "Prompt version" },
];

interface CostGroup {
  keys: string[];
  total: string | null;
  count: number;
}

export default function CostPage() {
  const workspace = useWorkspace();
  const [groupBy, setGroupBy] = useState("model");
  const window = useMemo(() => timeWindow(workspace.range), [workspace.range]);

  const overview = useQuery({
    queryKey: [
      "overview",
      "cost",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.overview({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        compare_previous: true,
      }),
  });

  const breakdown = useQuery({
    queryKey: [
      "costs",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
      groupBy,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.costs({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        group_by: [groupBy],
      }),
  });

  const spend = useQuery({
    queryKey: [
      "cost-series",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
      groupBy,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.timeseries({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        metric: "cost",
        aggregation: "sum",
        group_by: [groupBy],
        source: "spans",
      }),
  });

  const unpriced = useQuery({
    queryKey: [
      "unpriced",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.timeseries({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        aggregation: "count",
        group_by: ["model"],
        source: "spans",
        filter: ["cost_status:eq:unpriced"],
      }),
  });

  const tokens = useQuery({
    queryKey: [
      "token-series",
      workspace.projectId,
      workspace.environment,
      window.start,
      window.end,
    ],
    enabled: Boolean(workspace.projectId),
    queryFn: () =>
      api.timeseries({
        project_id: workspace.projectId!,
        environment: workspace.environment,
        start: window.start,
        end: window.end,
        metric: "total_tokens",
        aggregation: "sum",
        group_by: ["model"],
        source: "spans",
      }),
  });

  const summary = overview.data;
  const groups = (breakdown.data?.groups ?? []) as CostGroup[];
  const unpricedModels =
    unpriced.data?.groups.filter((group) => group.count > 0) ?? [];

  const columns: Column<CostGroup>[] = [
    {
      key: "group",
      header:
        GROUPINGS.find((item) => item.value === groupBy)?.label ?? "Group",
      render: (row) => row.keys.join(" / ") || "(unattributed)",
    },
    {
      key: "calls",
      header: "Calls",
      align: "right",
      render: (row) => formatNumber(row.count),
    },
    {
      key: "total",
      header: "Cost",
      align: "right",
      render: (row) => formatCost(row.total),
    },
    {
      key: "share",
      header: "Share",
      align: "right",
      render: (row) => {
        const total = groups.reduce(
          (sum, group) => sum + (Number.parseFloat(group.total ?? "0") || 0),
          0,
        );
        const value = Number.parseFloat(row.total ?? "0") || 0;
        // Share is a ratio, not money: computing it in floating point is fine
        // and it is only ever displayed as a rounded percentage.
        return total > 0 ? formatPercent(value / total) : "—";
      },
    },
    {
      key: "per_call",
      header: "Per call",
      align: "right",
      render: (row) =>
        row.count > 0 ? formatCost(divide(row.total, row.count)) : "—",
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Cost</h1>
          <p>
            Costs are computed from an effective-dated price book with exact
            decimal arithmetic. A model with no price entry is reported as
            unpriced, never as free.
          </p>
        </div>
        <div
          style={{ display: "flex", gap: "0.75rem", alignItems: "flex-end" }}
        >
          <Select
            label="Group by"
            value={groupBy}
            onChange={setGroupBy}
            options={GROUPINGS}
          />
          <Link href="/settings/price-books">Price books →</Link>
        </div>
      </header>

      {summary?.cost_is_partial && (
        <PartialDataNotice reason="At least one span in this window could not be priced. Every total on this page is therefore a lower bound." />
      )}
      {unpricedModels.length > 0 && (
        <PartialDataNotice
          reason={`Unpriced models in this window: ${unpricedModels
            .map(
              (group) =>
                `${group.keys.join("/") || "unknown"} (${group.count} call(s))`,
            )
            .join(", ")}. Add a price book entry to include them in totals.`}
        />
      )}

      {summary && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(12rem, 1fr))",
            gap: "1rem",
            marginBottom: "1rem",
          }}
        >
          <Stat
            label="Total cost"
            value={formatCost(summary.total_cost, summary.cost_currency)}
            detail={
              summary.cost_is_partial
                ? "lower bound — some usage is unpriced"
                : "all usage priced"
            }
            previous={
              summary.previous
                ? formatCost(
                    summary.previous.total_cost,
                    summary.previous.cost_currency,
                  )
                : undefined
            }
            invertDelta
          />
          <Stat
            label="Cost per request"
            value={
              summary.request_count > 0
                ? formatCost(
                    divide(summary.total_cost, summary.request_count),
                    summary.cost_currency,
                  )
                : "—"
            }
            detail={`${formatNumber(summary.request_count)} request(s)`}
          />
          <Stat
            label="Tokens"
            value={formatNumber(summary.total_tokens)}
            detail={`${formatNumber(summary.input_tokens)} in · ${formatNumber(summary.output_tokens)} out`}
          />
          <Stat
            label="Cost per 1k tokens"
            value={
              summary.total_tokens > 0
                ? formatCost(
                    divide(summary.total_cost, summary.total_tokens / 1000),
                    summary.cost_currency,
                  )
                : "—"
            }
            detail="blended across every model in the window"
          />
        </div>
      )}

      <Card title="Spend over time" subtitle={`grouped by ${groupBy}`}>
        {spend.isLoading && <Loading />}
        {spend.isError && (
          <ErrorState error={spend.error} onRetry={() => spend.refetch()} />
        )}
        {spend.data && (
          <TimeSeriesChart
            series={seriesFromDashboard(spend.data)}
            unit={spend.data.unit}
            partialBuckets={spend.data.partial_buckets}
            valueFormat="money"
            height={260}
          />
        )}
      </Card>

      <div
        style={{
          display: "grid",
          gap: "1rem",
          gridTemplateColumns: "repeat(auto-fit, minmax(22rem, 1fr))",
          marginTop: "1rem",
        }}
      >
        <Card title="Where the money goes">
          {breakdown.isLoading && <Loading />}
          {breakdown.isError && (
            <ErrorState
              error={breakdown.error}
              onRetry={() => breakdown.refetch()}
            />
          )}
          {breakdown.data && (
            <BarChart
              caption={`Cost grouped by ${groupBy}`}
              valueFormat="money"
              rows={groups
                .slice()
                .sort(
                  (a, b) =>
                    (Number.parseFloat(b.total ?? "0") || 0) -
                    (Number.parseFloat(a.total ?? "0") || 0),
                )
                .slice(0, 10)
                .map((group) => ({
                  label: group.keys.join(" / ") || "(unattributed)",
                  raw: group.total,
                  note: `${formatNumber(group.count)} calls`,
                }))}
            />
          )}
        </Card>

        <Card title="Token volume by model">
          {tokens.isLoading && <Loading />}
          {tokens.isError && (
            <ErrorState error={tokens.error} onRetry={() => tokens.refetch()} />
          )}
          {tokens.data && (
            <TimeSeriesChart
              series={seriesFromDashboard(tokens.data)}
              unit={tokens.data.unit}
              partialBuckets={tokens.data.partial_buckets}
            />
          )}
        </Card>
      </div>

      <div style={{ marginTop: "1rem" }}>
        <Card title="Breakdown" padded={false}>
          {breakdown.data && (
            <DataTable
              columns={columns}
              rows={groups}
              rowKey={(row) => row.keys.join("|") || "none"}
              caption={`Cost grouped by ${groupBy}`}
              emptyMessage="No priced usage in this window"
            />
          )}
        </Card>
      </div>
    </>
  );
}

/**
 * Divide a decimal-string amount by a count, returning a decimal string.
 *
 * Done with integers on the scaled value rather than with `Number` division so
 * a per-call cost of $0.0000004 does not round to zero on the way through a
 * float. Precision beyond 12 fractional digits is dropped, which is far below
 * anything a price book expresses.
 */
function divide(
  amount: string | null | undefined,
  divisor: number,
): string | null {
  if (!amount || !Number.isFinite(divisor) || divisor === 0) return null;
  const negative = amount.startsWith("-");
  const digits = negative ? amount.slice(1) : amount;
  const [integerPart = "0", fractionPart = ""] = digits.split(".");
  const scale = 12;
  const scaled = BigInt(
    integerPart + fractionPart.padEnd(scale, "0").slice(0, scale),
  );
  // Scale the divisor too, so a fractional divisor (tokens / 1000) is exact
  // enough at six decimal places.
  const divisorScaled = BigInt(Math.round(divisor * 1_000_000));
  if (divisorScaled === 0n) return null;
  const quotient = (scaled * 1_000_000n) / divisorScaled;
  const text = quotient.toString().padStart(scale + 1, "0");
  const whole = text.slice(0, text.length - scale);
  const fraction = text.slice(text.length - scale);
  return `${negative ? "-" : ""}${whole}.${fraction}`;
}
