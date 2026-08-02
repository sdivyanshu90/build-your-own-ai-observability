import { describe, expect, it } from "vitest";

import {
  describeDelta,
  formatCompact,
  formatCost,
  formatDuration,
  formatNumber,
  formatPercent,
  shortId,
  timeWindow,
} from "@/lib/format";

describe("formatCost", () => {
  it("never converts money to a binary float", () => {
    // 0.0000001 has no exact float representation; going through Number would
    // render 1.0000000000000001e-7.
    expect(formatCost("0.0000001")).toBe("$0.00000010");
    expect(formatCost("0.1")).toBe("$0.10");
    expect(formatCost("0.000000000123")).toBe("$0.0000000001");
  });

  it("keeps sub-cent precision instead of collapsing to $0.00", () => {
    expect(formatCost("0.000345")).toBe("$0.00034");
    expect(formatCost("0.0000000001")).not.toBe("$0.00");
  });

  it("distinguishes unknown from zero", () => {
    expect(formatCost(null)).toBe("—");
    expect(formatCost(undefined)).toBe("—");
    expect(formatCost("")).toBe("—");
    expect(formatCost("0")).toBe("$0.00");
    expect(formatCost("0.00")).toBe("$0.00");
  });

  it("groups whole units and preserves the sign", () => {
    expect(formatCost("1234.5678")).toBe("$1,234.56");
    expect(formatCost("-42.5")).toBe("-$42.50");
  });

  it("handles a very large amount without exponent notation", () => {
    expect(formatCost("987654321.99")).toBe("$987,654,321.99");
  });

  it("labels non-USD currencies explicitly", () => {
    expect(formatCost("12.34", "EUR")).toBe("EUR 12.34");
  });
});

describe("formatDuration", () => {
  it("scales units so a value is always readable", () => {
    expect(formatDuration(0.4)).toBe("400µs");
    expect(formatDuration(4.25)).toBe("4.3ms");
    expect(formatDuration(250)).toBe("250ms");
    expect(formatDuration(1500)).toBe("1.50s");
    expect(formatDuration(125_000)).toBe("2m 5s");
  });

  it("renders unknown as an em dash, not as zero", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(0)).toBe("0µs");
  });
});

describe("formatNumber and formatCompact", () => {
  it("distinguishes unknown from zero", () => {
    expect(formatNumber(null)).toBe("—");
    expect(formatNumber(0)).toBe("0");
    expect(formatCompact(null)).toBe("—");
  });

  it("groups thousands", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
    expect(formatCompact(1234567)).toBe("1.2M");
  });
});

describe("formatPercent", () => {
  it("formats a ratio with the requested precision", () => {
    expect(formatPercent(0.12345)).toBe("12.3%");
    expect(formatPercent(0.12345, 3)).toBe("12.345%");
    expect(formatPercent(null)).toBe("—");
  });
});

describe("describeDelta", () => {
  it("states direction in words, not only as a sign", () => {
    expect(describeDelta(5, 0.25)).toEqual({
      label: "up (25.0%)",
      direction: "up",
    });
    expect(describeDelta(-5, -0.25)).toEqual({
      label: "down (25.0%)",
      direction: "down",
    });
  });

  it("reports no change as flat rather than as a zero percent rise", () => {
    expect(describeDelta(0, 0)).toEqual({
      label: "unchanged",
      direction: "flat",
    });
    expect(describeDelta(null, null)).toEqual({
      label: "unchanged",
      direction: "flat",
    });
  });

  it("omits the percentage when the baseline was zero", () => {
    expect(describeDelta(7, null)).toEqual({ label: "up", direction: "up" });
  });
});

describe("shortId", () => {
  it("truncates only when needed", () => {
    expect(shortId("abc")).toBe("abc");
    expect(shortId("0123456789abcdef", 8)).toBe("01234567…");
  });
});

describe("timeWindow", () => {
  it("produces an RFC 3339 window of the requested width", () => {
    const window = timeWindow("1h");
    const start = new Date(window.start).getTime();
    const end = new Date(window.end).getTime();
    expect(end - start).toBe(3_600_000);
    expect(window.start).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it("falls back to an hour for an unknown range rather than throwing", () => {
    const window = timeWindow("not-a-range");
    const width =
      new Date(window.end).getTime() - new Date(window.start).getTime();
    expect(width).toBe(3_600_000);
  });
});
