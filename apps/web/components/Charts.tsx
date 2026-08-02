"use client";

/**
 * Charts.
 *
 * Hand-rolled SVG rather than a charting library, for three reasons that matter
 * more here than the convenience would:
 *
 * 1. **Partial buckets must be visibly different.** The most recent bucket of
 *    any time series is still filling. Drawn normally it looks like a cliff, and
 *    people page each other over it. Every series here renders its partial tail
 *    dashed and hollow, and the legend says so.
 * 2. **Money stays a string.** Cost series arrive as decimal strings; a library
 *    would coerce them to floats. Values are parsed to a float *only* for the
 *    pixel position, never for the label, which is rendered from the original
 *    string.
 * 3. **Accessibility.** Each chart is also a table for screen readers, and each
 *    series is identified by a marker shape as well as a colour.
 */

import { useId, useMemo, useState } from "react";
import type { DashboardSeries, MetricGroup } from "@aiobs/schemas";

import { formatCost, formatCompact, formatTimestamp } from "@/lib/format";
import { EmptyState, SERIES_COLOURS } from "./ui";

const MARKERS = ["circle", "square", "triangle", "diamond", "cross"] as const;

export interface ChartPoint {
  bucket: string;
  /** Original value as sent by the API. Strings are money. */
  raw: number | string | null;
  /** Numeric projection used only for geometry. */
  value: number | null;
}

export interface ChartSeries {
  key: string;
  label: string;
  points: ChartPoint[];
}

function toNumber(value: number | string | null): number | null {
  if (value === null) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function seriesFromDashboard(series: DashboardSeries): ChartSeries[] {
  return series.groups.map((group: MetricGroup, index) => ({
    key: group.keys.join(" / ") || `series-${index}`,
    label: group.keys.join(" / ") || series.metric,
    points: group.points.map((point) => ({
      bucket: point.bucket,
      raw: point.value,
      value: toNumber(point.value),
    })),
  }));
}

/**
 * Multi-series time chart.
 *
 * `partialBuckets` comes straight from the API rather than being guessed from
 * the clock: the server knows its own bucket boundaries and ingestion lag, the
 * browser does not.
 */
export function TimeSeriesChart({
  series,
  unit,
  partialBuckets = [],
  height = 220,
  valueFormat = "number",
  currency = "USD",
}: {
  series: ChartSeries[];
  unit: string;
  partialBuckets?: string[];
  height?: number;
  valueFormat?: "number" | "money" | "ms" | "percent";
  currency?: string;
}) {
  const titleId = useId();
  const [hover, setHover] = useState<number | null>(null);

  const buckets = useMemo(() => {
    const all = new Set<string>();
    for (const item of series)
      for (const point of item.points) all.add(point.bucket);
    return [...all].sort();
  }, [series]);

  const partial = useMemo(() => new Set(partialBuckets), [partialBuckets]);

  const max = useMemo(() => {
    let value = 0;
    for (const item of series) {
      for (const point of item.points)
        if (point.value !== null && point.value > value) value = point.value;
    }
    // A flat-zero chart still needs a sane axis rather than a divide by zero.
    return value === 0 ? 1 : value * 1.12;
  }, [series]);

  if (buckets.length === 0 || series.length === 0) {
    return (
      <EmptyState
        title="No data in this window"
        description="Widen the time range or relax the filters."
      />
    );
  }

  const width = 720;
  const padding = { top: 12, right: 12, bottom: 26, left: 56 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const xFor = (index: number) =>
    padding.left +
    (buckets.length === 1
      ? plotWidth / 2
      : (index / (buckets.length - 1)) * plotWidth);
  const yFor = (value: number) =>
    padding.top + plotHeight - (value / max) * plotHeight;

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => max * fraction);

  return (
    <figure style={{ margin: 0 }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-labelledby={titleId}
        preserveAspectRatio="none"
        onMouseLeave={() => setHover(null)}
      >
        <title id={titleId}>
          {series.length} series over {buckets.length} time buckets, measured in{" "}
          {unit}
        </title>

        {ticks.map((tick, index) => (
          <g key={index}>
            <line
              x1={padding.left}
              x2={width - padding.right}
              y1={yFor(tick)}
              y2={yFor(tick)}
              stroke="var(--border)"
              strokeWidth={1}
            />
            <text
              x={padding.left - 6}
              y={yFor(tick) + 3}
              textAnchor="end"
              fontSize={10}
              fill="var(--text-faint)"
            >
              {formatAxis(tick, valueFormat)}
            </text>
          </g>
        ))}

        {series.map((item, seriesIndex) => {
          const colour = SERIES_COLOURS[seriesIndex % SERIES_COLOURS.length]!;
          const byBucket = new Map(
            item.points.map((point) => [point.bucket, point]),
          );
          const segments = buildSegments(buckets, byBucket, partial);
          return (
            <g key={item.key}>
              {segments.map((segment, index) => (
                <polyline
                  key={index}
                  fill="none"
                  stroke={colour}
                  strokeWidth={2}
                  strokeDasharray={segment.partial ? "4 3" : undefined}
                  strokeLinejoin="round"
                  points={segment.indices
                    .map((bucketIndex) => {
                      const point = byBucket.get(buckets[bucketIndex]!);
                      return `${xFor(bucketIndex)},${yFor(point?.value ?? 0)}`;
                    })
                    .join(" ")}
                />
              ))}
              {buckets.map((bucket, index) => {
                const point = byBucket.get(bucket);
                if (!point || point.value === null) return null;
                return (
                  <Marker
                    key={bucket}
                    shape={MARKERS[seriesIndex % MARKERS.length]!}
                    x={xFor(index)}
                    y={yFor(point.value)}
                    colour={colour}
                    hollow={partial.has(bucket)}
                  />
                );
              })}
            </g>
          );
        })}

        {buckets.map((bucket, index) => (
          <rect
            key={bucket}
            x={xFor(index) - plotWidth / Math.max(buckets.length, 1) / 2}
            y={padding.top}
            width={plotWidth / Math.max(buckets.length, 1)}
            height={plotHeight}
            fill="transparent"
            onMouseEnter={() => setHover(index)}
          />
        ))}

        {hover !== null && (
          <line
            x1={xFor(hover)}
            x2={xFor(hover)}
            y1={padding.top}
            y2={padding.top + plotHeight}
            stroke="var(--text-faint)"
            strokeDasharray="2 2"
          />
        )}

        <text
          x={padding.left}
          y={height - 8}
          fontSize={10}
          fill="var(--text-faint)"
        >
          {shortTime(buckets[0]!)}
        </text>
        <text
          x={width - padding.right}
          y={height - 8}
          fontSize={10}
          fill="var(--text-faint)"
          textAnchor="end"
        >
          {shortTime(buckets[buckets.length - 1]!)}
        </text>
      </svg>

      <figcaption style={{ marginTop: "0.5rem" }}>
        <Legend series={series} />
        {partial.size > 0 && (
          <p
            style={{
              margin: "0.375rem 0 0",
              fontSize: "0.6875rem",
              color: "var(--text-muted)",
            }}
          >
            <span aria-hidden="true">┄ </span>
            Dashed segments and hollow markers are buckets that are still
            filling. Their values will rise.
          </p>
        )}
        {hover !== null && (
          <HoverReadout
            bucket={buckets[hover]!}
            series={series}
            partial={partial.has(buckets[hover]!)}
            valueFormat={valueFormat}
            currency={currency}
          />
        )}
      </figcaption>

      <table className="sr-only">
        <caption>Chart data in {unit}</caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            {series.map((item) => (
              <th key={item.key} scope="col">
                {item.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr key={bucket}>
              <th scope="row">
                {formatTimestamp(bucket)}
                {partial.has(bucket) ? " (partial)" : ""}
              </th>
              {series.map((item) => {
                const point = item.points.find(
                  (candidate) => candidate.bucket === bucket,
                );
                return (
                  <td key={item.key}>
                    {formatValue(point?.raw ?? null, valueFormat, currency)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

function HoverReadout({
  bucket,
  series,
  partial,
  valueFormat,
  currency,
}: {
  bucket: string;
  series: ChartSeries[];
  partial: boolean;
  valueFormat: "number" | "money" | "ms" | "percent";
  currency: string;
}) {
  return (
    <div
      style={{
        marginTop: "0.5rem",
        padding: "0.5rem 0.625rem",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        background: "var(--bg-subtle)",
        fontSize: "0.75rem",
      }}
    >
      <strong>{formatTimestamp(bucket)}</strong>
      {partial && (
        <span style={{ color: "var(--warn)" }}> · still filling</span>
      )}
      <ul style={{ margin: "0.25rem 0 0", padding: 0, listStyle: "none" }}>
        {series.map((item, index) => {
          const point = item.points.find(
            (candidate) => candidate.bucket === bucket,
          );
          return (
            <li
              key={item.key}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: "1rem",
              }}
            >
              <span>
                <span
                  aria-hidden="true"
                  style={{
                    display: "inline-block",
                    width: "0.5rem",
                    height: "0.5rem",
                    background: SERIES_COLOURS[index % SERIES_COLOURS.length],
                    marginRight: "0.375rem",
                  }}
                />
                {item.label}
              </span>
              <span>
                {formatValue(point?.raw ?? null, valueFormat, currency)}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Legend({ series }: { series: ChartSeries[] }) {
  return (
    <ul
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: "0.75rem",
        listStyle: "none",
        margin: 0,
        padding: 0,
        fontSize: "0.75rem",
      }}
    >
      {series.map((item, index) => (
        <li
          key={item.key}
          style={{ display: "flex", alignItems: "center", gap: "0.375rem" }}
        >
          <svg width="12" height="12" aria-hidden="true">
            <Marker
              shape={MARKERS[index % MARKERS.length]!}
              x={6}
              y={6}
              colour={SERIES_COLOURS[index % SERIES_COLOURS.length]!}
            />
          </svg>
          {item.label}
        </li>
      ))}
    </ul>
  );
}

function Marker({
  shape,
  x,
  y,
  colour,
  hollow = false,
}: {
  shape: (typeof MARKERS)[number];
  x: number;
  y: number;
  colour: string;
  hollow?: boolean;
}) {
  const fill = hollow ? "var(--bg)" : colour;
  const common = { fill, stroke: colour, strokeWidth: 1.5 };
  switch (shape) {
    case "square":
      return <rect x={x - 3} y={y - 3} width={6} height={6} {...common} />;
    case "triangle":
      return (
        <polygon
          points={`${x},${y - 4} ${x + 4},${y + 3} ${x - 4},${y + 3}`}
          {...common}
        />
      );
    case "diamond":
      return (
        <polygon
          points={`${x},${y - 4} ${x + 4},${y} ${x},${y + 4} ${x - 4},${y}`}
          {...common}
        />
      );
    case "cross":
      return (
        <g stroke={colour} strokeWidth={1.8}>
          <line x1={x - 3} y1={y - 3} x2={x + 3} y2={y + 3} />
          <line x1={x - 3} y1={y + 3} x2={x + 3} y2={y - 3} />
        </g>
      );
    default:
      return <circle cx={x} cy={y} r={3.2} {...common} />;
  }
}

/** Horizontal bar chart for grouped totals (cost by model, errors by service). */
export function BarChart({
  rows,
  valueFormat = "number",
  currency = "USD",
  caption,
}: {
  rows: { label: string; raw: number | string | null; note?: string }[];
  valueFormat?: "number" | "money" | "ms" | "percent";
  currency?: string;
  caption: string;
}) {
  const values = rows.map((row) => toNumber(row.raw) ?? 0);
  const max = Math.max(1, ...values);
  if (rows.length === 0) return <EmptyState title="No data" />;
  return (
    <table
      style={{
        width: "100%",
        borderCollapse: "collapse",
        fontSize: "0.8125rem",
      }}
    >
      <caption className="sr-only">{caption}</caption>
      <tbody>
        {rows.map((row, index) => {
          const value = values[index]!;
          return (
            <tr key={row.label}>
              <th
                scope="row"
                style={{
                  textAlign: "left",
                  fontWeight: 500,
                  padding: "0.25rem 0.5rem 0.25rem 0",
                  maxWidth: "14rem",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
                title={row.label}
              >
                {row.label}
              </th>
              <td style={{ width: "100%", padding: "0.25rem 0" }}>
                <div
                  style={{
                    height: "0.75rem",
                    borderRadius: "2px",
                    width: `${Math.max(2, (value / max) * 100)}%`,
                    background: SERIES_COLOURS[index % SERIES_COLOURS.length],
                  }}
                />
              </td>
              <td
                style={{
                  padding: "0.25rem 0 0.25rem 0.5rem",
                  whiteSpace: "nowrap",
                  textAlign: "right",
                }}
              >
                {formatValue(row.raw, valueFormat, currency)}
                {row.note && (
                  <span
                    style={{
                      color: "var(--text-faint)",
                      marginLeft: "0.375rem",
                    }}
                  >
                    {row.note}
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Split a bucket list into complete and partial runs so each can be stroked
 *  differently. The partial run includes the last complete point so the line
 *  stays connected. */
function buildSegments(
  buckets: string[],
  byBucket: Map<string, ChartPoint>,
  partial: Set<string>,
): { indices: number[]; partial: boolean }[] {
  const segments: { indices: number[]; partial: boolean }[] = [];
  let current: { indices: number[]; partial: boolean } | null = null;

  buckets.forEach((bucket, index) => {
    const point = byBucket.get(bucket);
    if (!point || point.value === null) {
      current = null;
      return;
    }
    const isPartial = partial.has(bucket);
    if (!current || current.partial !== isPartial) {
      const previous = current;
      current = { indices: [], partial: isPartial };
      if (previous && previous.indices.length > 0) {
        current.indices.push(previous.indices[previous.indices.length - 1]!);
      }
      segments.push(current);
    }
    current.indices.push(index);
  });

  return segments.filter((segment) => segment.indices.length > 1);
}

function formatAxis(
  value: number,
  kind: "number" | "money" | "ms" | "percent",
): string {
  if (kind === "ms") return `${formatCompact(value)}ms`;
  if (kind === "percent") return `${(value * 100).toFixed(0)}%`;
  if (kind === "money")
    return `$${value < 1 ? value.toFixed(4) : formatCompact(value)}`;
  return formatCompact(value);
}

function formatValue(
  raw: number | string | null,
  kind: "number" | "money" | "ms" | "percent",
  currency: string,
): string {
  if (raw === null) return "—";
  if (kind === "money")
    return formatCost(typeof raw === "string" ? raw : String(raw), currency);
  const numeric = toNumber(raw);
  if (numeric === null) return "—";
  if (kind === "ms") return `${numeric.toFixed(numeric < 10 ? 1 : 0)}ms`;
  if (kind === "percent") return `${(numeric * 100).toFixed(2)}%`;
  return new Intl.NumberFormat("en-US").format(numeric);
}

function shortTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
}
