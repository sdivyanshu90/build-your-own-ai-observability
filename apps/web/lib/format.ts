/**
 * Presentation helpers.
 *
 * Rules that are easy to get wrong and matter:
 *
 * - **Money is never a JavaScript number.** The API sends decimal strings; they
 *   are formatted as strings and never parsed into a float for display, or a
 *   $0.0000001 per-token price becomes 1.0000000000000001e-7.
 * - **Sub-cent precision is preserved.** Two decimal places would render almost
 *   every individual AI request as "$0.00", which is useless.
 * - **Unknown is not zero.** `null` renders as an em dash, never as 0, so a
 *   missing token count is visibly different from a genuine zero.
 */

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1) return `${(ms * 1000).toFixed(0)}µs`;
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 1 : 0)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${((ms % 60_000) / 1000).toFixed(0)}s`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

/**
 * Format a decimal-string cost.
 *
 * Takes a string and returns a string: the value never becomes a float.
 * Very small amounts keep enough significant digits to be meaningful rather
 * than collapsing to zero.
 */
export function formatCost(
  value: string | null | undefined,
  currency = "USD",
): string {
  if (value === null || value === undefined || value === "") return "—";
  const negative = value.startsWith("-");
  const digits = negative ? value.slice(1) : value;
  const [integerPart = "0", fractionPart = ""] = digits.split(".");

  const symbol = currency === "USD" ? "$" : `${currency} `;
  const isZeroInteger = /^0*$/.test(integerPart);

  if (!isZeroInteger) {
    const grouped = Number.parseInt(integerPart, 10).toLocaleString("en-US");
    return `${negative ? "-" : ""}${symbol}${grouped}.${(fractionPart + "00").slice(0, 2)}`;
  }

  // Below one unit: keep digits until the first two significant ones, so a
  // fraction of a cent is still readable.
  const firstSignificant = fractionPart.search(/[1-9]/);
  if (firstSignificant === -1) return `${symbol}0.00`;
  const places = Math.min(Math.max(firstSignificant + 2, 2), 10);
  return `${negative ? "-" : ""}${symbol}0.${fractionPart.slice(0, places).padEnd(places, "0")}`;
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-GB", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

export function shortId(value: string, length = 12): string {
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}

export function formatPercent(
  value: number | null | undefined,
  places = 1,
): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(places)}%`;
}

/**
 * Describe a change between two values in words as well as a sign.
 *
 * Colour alone must never carry the meaning: the direction is stated in text
 * for anyone who cannot distinguish red from green.
 */
export function describeDelta(
  absolute: number | null,
  relative: number | null,
): { label: string; direction: "up" | "down" | "flat" } {
  if (absolute === null || absolute === 0)
    return { label: "unchanged", direction: "flat" };
  const direction = absolute > 0 ? "up" : "down";
  const percent =
    relative === null ? "" : ` (${Math.abs(relative * 100).toFixed(1)}%)`;
  return {
    label: `${direction === "up" ? "up" : "down"}${percent}`,
    direction,
  };
}

export const ISO = (date: Date): string =>
  date.toISOString().replace(/\.\d{3}Z$/, "Z");

export function timeWindow(range: string): { start: string; end: string } {
  const end = new Date();
  const minutes: Record<string, number> = {
    "15m": 15,
    "1h": 60,
    "6h": 360,
    "24h": 1_440,
    "7d": 10_080,
    "30d": 43_200,
  };
  const start = new Date(end.getTime() - (minutes[range] ?? 60) * 60_000);
  return { start: ISO(start), end: ISO(end) };
}

export const TIME_RANGES = ["15m", "1h", "6h", "24h", "7d", "30d"] as const;
export type TimeRange = (typeof TIME_RANGES)[number];
