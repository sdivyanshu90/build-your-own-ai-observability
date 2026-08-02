import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { DashboardSeries } from "@aiobs/schemas";

import {
  BarChart,
  TimeSeriesChart,
  seriesFromDashboard,
} from "@/components/Charts";

const SERIES: DashboardSeries = {
  metric: "requests",
  aggregation: "count",
  interval: "1h",
  unit: "count",
  partial_buckets: ["2026-03-01T12:00:00Z"],
  groups: [
    {
      keys: ["gpt-4o"],
      total: 30,
      count: 3,
      points: [
        { bucket: "2026-03-01T10:00:00Z", value: 12, count: 12 },
        { bucket: "2026-03-01T11:00:00Z", value: 15, count: 15 },
        { bucket: "2026-03-01T12:00:00Z", value: 3, count: 3 },
      ],
    },
    {
      keys: ["claude-sonnet-5"],
      total: 21,
      count: 3,
      points: [
        { bucket: "2026-03-01T10:00:00Z", value: 9, count: 9 },
        { bucket: "2026-03-01T11:00:00Z", value: 10, count: 10 },
        { bucket: "2026-03-01T12:00:00Z", value: 2, count: 2 },
      ],
    },
  ],
};

describe("seriesFromDashboard", () => {
  it("keeps the original value alongside its numeric projection", () => {
    const money: DashboardSeries = {
      ...SERIES,
      metric: "cost",
      groups: [
        {
          keys: ["gpt-4o"],
          total: "0.0000001",
          count: 1,
          points: [
            { bucket: "2026-03-01T10:00:00Z", value: "0.0000001", count: 1 },
          ],
        },
      ],
    };
    const [series] = seriesFromDashboard(money);
    expect(series!.points[0]!.raw).toBe("0.0000001");
    expect(series!.points[0]!.value).toBeCloseTo(1e-7);
  });

  it("labels an ungrouped series with the metric name", () => {
    const ungrouped: DashboardSeries = {
      ...SERIES,
      groups: [
        {
          keys: [],
          total: 1,
          count: 1,
          points: [{ bucket: "2026-03-01T10:00:00Z", value: 1, count: 1 }],
        },
      ],
    };
    expect(seriesFromDashboard(ungrouped)[0]!.label).toBe("requests");
  });
});

describe("TimeSeriesChart", () => {
  it("renders an accessible data table alongside the drawing", () => {
    render(
      <TimeSeriesChart
        series={seriesFromDashboard(SERIES)}
        unit="count"
        partialBuckets={SERIES.partial_buckets}
      />,
    );
    const table = screen.getByRole("table");
    expect(within(table).getByText("gpt-4o")).toBeInTheDocument();
    expect(within(table).getByText("claude-sonnet-5")).toBeInTheDocument();
  });

  it("marks the still-filling bucket as partial in the table and in prose", () => {
    render(
      <TimeSeriesChart
        series={seriesFromDashboard(SERIES)}
        unit="count"
        partialBuckets={SERIES.partial_buckets}
      />,
    );
    expect(screen.getByText(/still filling/)).toBeInTheDocument();
    expect(screen.getByText(/\(partial\)/)).toBeInTheDocument();
  });

  it("draws the partial tail with a dash pattern so it is not read as a cliff", () => {
    const { container } = render(
      <TimeSeriesChart
        series={seriesFromDashboard(SERIES)}
        unit="count"
        partialBuckets={SERIES.partial_buckets}
      />,
    );
    expect(
      container.querySelectorAll("polyline[stroke-dasharray]").length,
    ).toBeGreaterThan(0);
  });

  it("formats money from the decimal string, not from a float", () => {
    const money: DashboardSeries = {
      ...SERIES,
      partial_buckets: [],
      groups: [
        {
          keys: ["gpt-4o"],
          total: "0.0000001",
          count: 1,
          points: [
            { bucket: "2026-03-01T10:00:00Z", value: "0.0000001", count: 1 },
          ],
        },
      ],
    };
    render(
      <TimeSeriesChart
        series={seriesFromDashboard(money)}
        unit="USD"
        valueFormat="money"
      />,
    );
    expect(screen.getByText("$0.00000010")).toBeInTheDocument();
  });

  it("shows an empty state rather than an axis with no data", () => {
    render(<TimeSeriesChart series={[]} unit="count" />);
    expect(screen.getByText("No data in this window")).toBeInTheDocument();
  });

  it("survives an all-zero series without dividing by zero", () => {
    const zeros: DashboardSeries = {
      ...SERIES,
      partial_buckets: [],
      groups: [
        {
          keys: ["idle"],
          total: 0,
          count: 2,
          points: [
            { bucket: "2026-03-01T10:00:00Z", value: 0, count: 0 },
            { bucket: "2026-03-01T11:00:00Z", value: 0, count: 0 },
          ],
        },
      ],
    };
    const { container } = render(
      <TimeSeriesChart series={seriesFromDashboard(zeros)} unit="count" />,
    );
    expect(container.innerHTML).not.toContain("NaN");
  });

  it("renders a single-point series without collapsing the scale", () => {
    const single: DashboardSeries = {
      ...SERIES,
      partial_buckets: [],
      groups: [
        {
          keys: ["one"],
          total: 5,
          count: 1,
          points: [{ bucket: "2026-03-01T10:00:00Z", value: 5, count: 5 }],
        },
      ],
    };
    const { container } = render(
      <TimeSeriesChart series={seriesFromDashboard(single)} unit="count" />,
    );
    expect(container.innerHTML).not.toContain("NaN");
  });
});

describe("BarChart", () => {
  it("renders values from decimal strings", () => {
    render(
      <BarChart
        caption="Cost by model"
        valueFormat="money"
        rows={[
          { label: "gpt-4o", raw: "12.3456" },
          { label: "claude-sonnet-5", raw: "0.00004" },
        ]}
      />,
    );
    expect(screen.getByText("$12.34")).toBeInTheDocument();
    expect(screen.getByText("$0.000040")).toBeInTheDocument();
  });

  it("shows an empty state for no rows", () => {
    render(<BarChart caption="none" rows={[]} />);
    expect(screen.getByText("No data")).toBeInTheDocument();
  });
});
