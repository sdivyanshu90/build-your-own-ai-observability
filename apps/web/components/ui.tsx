"use client";

/**
 * Shared UI primitives.
 *
 * Three rules run through all of them:
 *
 * 1. **Every state is designed.** Loading, empty, partial and error are not
 *    afterthoughts -- a dashboard that renders nothing when a query fails is
 *    indistinguishable from one showing zero traffic, and that ambiguity costs
 *    real time during an incident.
 * 2. **Status is never colour alone.** Every badge carries text; every chart
 *    series is labelled.
 * 3. **User content is rendered as text, never as markup.** Prompts, tool
 *    outputs and retrieved documents are attacker-influenced by definition.
 */

import { useId } from "react";
import type { CSSProperties, ReactNode } from "react";

import { describeDelta } from "@/lib/format";

// ---------------------------------------------------------------------------
// layout
// ---------------------------------------------------------------------------

export function Card({
  title,
  subtitle,
  actions,
  children,
  padded = true,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  padded?: boolean;
}) {
  return (
    <section
      style={{
        background: "var(--bg-raised)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
        boxShadow: "var(--shadow)",
        overflow: "hidden",
      }}
    >
      {(title || actions) && (
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "1rem",
            padding: "0.75rem 1rem",
            borderBottom: "1px solid var(--border)",
            background: "var(--bg-subtle)",
          }}
        >
          <div style={{ minWidth: 0 }}>
            <h2 style={{ margin: 0, fontSize: "0.875rem", fontWeight: 600 }}>
              {title}
            </h2>
            {subtitle && (
              <p
                style={{
                  margin: "0.125rem 0 0",
                  fontSize: "0.75rem",
                  color: "var(--text-muted)",
                }}
              >
                {subtitle}
              </p>
            )}
          </div>
          {actions}
        </header>
      )}
      <div style={{ padding: padded ? "1rem" : 0 }}>{children}</div>
    </section>
  );
}

export function Grid({
  columns = 4,
  children,
}: {
  columns?: number;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(auto-fit, minmax(${Math.floor(1100 / columns)}px, 1fr))`,
        gap: "1rem",
      }}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        padding: "2rem",
        textAlign: "center",
        color: "var(--text-muted)",
      }}
    >
      {label}…
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div style={{ padding: "2.5rem 1rem", textAlign: "center" }}>
      <p style={{ margin: "0 0 0.25rem", fontWeight: 600 }}>{title}</p>
      {description && (
        <p
          style={{
            margin: "0 0 1rem",
            color: "var(--text-muted)",
            maxWidth: "46ch",
            marginInline: "auto",
          }}
        >
          {description}
        </p>
      )}
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const message = error instanceof Error ? error.message : String(error);
  const code =
    typeof error === "object" && error !== null && "body" in error
      ? String((error as { body?: { code?: string } }).body?.code ?? "")
      : "";
  const requestId =
    typeof error === "object" && error !== null && "body" in error
      ? String(
          (error as { body?: { request_id?: string } }).body?.request_id ?? "",
        )
      : "";

  return (
    <div
      role="alert"
      style={{
        padding: "1rem",
        background: "var(--error-subtle)",
        border: "1px solid var(--error)",
        borderRadius: "var(--radius)",
      }}
    >
      <p
        style={{
          margin: "0 0 0.25rem",
          fontWeight: 600,
          color: "var(--error)",
        }}
      >
        Could not load this data
      </p>
      <p style={{ margin: "0 0 0.5rem" }}>{message}</p>
      {code && (
        <p
          style={{
            margin: 0,
            fontFamily: "var(--mono)",
            fontSize: "0.75rem",
            color: "var(--text-muted)",
          }}
        >
          {code}
          {requestId ? ` · request ${requestId}` : ""}
        </p>
      )}
      {onRetry && (
        <Button onClick={onRetry} style={{ marginTop: "0.75rem" }}>
          Try again
        </Button>
      )}
    </div>
  );
}

/**
 * Warns that a figure is incomplete.
 *
 * Shown when a window's most recent bucket is still filling, or when some
 * contributing costs were estimated. Silently rendering a partial number as a
 * final one is how a dashboard produces a false incident.
 */
export function PartialDataNotice({ reason }: { reason: string }) {
  return (
    <p
      role="note"
      style={{
        margin: "0.5rem 0 0",
        fontSize: "0.75rem",
        color: "var(--warn)",
        display: "flex",
        gap: "0.375rem",
        alignItems: "center",
      }}
    >
      <span aria-hidden="true">⚠</span>
      {reason}
    </p>
  );
}

// ---------------------------------------------------------------------------
// atoms
// ---------------------------------------------------------------------------

const STATUS_STYLES: Record<string, { bg: string; fg: string; icon: string }> =
  {
    ok: { bg: "var(--ok-subtle)", fg: "var(--ok)", icon: "✓" },
    error: { bg: "var(--error-subtle)", fg: "var(--error)", icon: "✕" },
    incomplete: { bg: "var(--warn-subtle)", fg: "var(--warn)", icon: "◌" },
    unset: { bg: "var(--bg-hover)", fg: "var(--text-muted)", icon: "·" },
    final: { bg: "var(--ok-subtle)", fg: "var(--ok)", icon: "✓" },
    estimated: { bg: "var(--warn-subtle)", fg: "var(--warn)", icon: "≈" },
    unpriced: { bg: "var(--bg-hover)", fg: "var(--text-muted)", icon: "?" },
    provider: { bg: "var(--ok-subtle)", fg: "var(--ok)", icon: "✓" },
    missing: { bg: "var(--bg-hover)", fg: "var(--text-muted)", icon: "?" },
    reconciled: { bg: "var(--info-subtle)", fg: "var(--info)", icon: "↻" },
  };

/**
 * A status label.
 *
 * The icon and the word both encode the state, so the badge is readable
 * without colour perception and by a screen reader.
 */
export function StatusBadge({
  status,
  title,
}: {
  status: string;
  title?: string;
}) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES["unset"]!;
  return (
    <span
      title={title ?? status}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.25rem",
        padding: "0.0625rem 0.4rem",
        borderRadius: "999px",
        background: style.bg,
        color: style.fg,
        fontSize: "0.75rem",
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      <span aria-hidden="true">{style.icon}</span>
      {status}
    </span>
  );
}

export function Tag({ children }: { children: ReactNode }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "0.0625rem 0.4rem",
        borderRadius: "var(--radius)",
        background: "var(--bg-hover)",
        color: "var(--text-muted)",
        fontSize: "0.75rem",
        marginRight: "0.25rem",
      }}
    >
      {children}
    </span>
  );
}

export function Mono({
  children,
  title,
}: {
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      style={{ fontFamily: "var(--mono)", fontSize: "0.8125rem" }}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "default",
  type = "button",
  disabled,
  style,
  ...rest
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  type?: "button" | "submit";
  disabled?: boolean;
  style?: CSSProperties;
} & Record<string, unknown>) {
  const palette: Record<string, CSSProperties> = {
    default: {
      background: "var(--bg-raised)",
      color: "var(--text)",
      borderColor: "var(--border-strong)",
    },
    primary: {
      background: "var(--accent)",
      color: "var(--accent-fg)",
      borderColor: "var(--accent)",
    },
    danger: {
      background: "var(--error-subtle)",
      color: "var(--error)",
      borderColor: "var(--error)",
    },
    ghost: {
      background: "transparent",
      color: "var(--text-muted)",
      borderColor: "transparent",
    },
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "0.375rem 0.75rem",
        borderRadius: "var(--radius)",
        border: "1px solid",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.55 : 1,
        fontSize: "0.8125rem",
        fontWeight: 500,
        fontFamily: "inherit",
        ...palette[variant],
        ...style,
      }}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Select({
  label,
  value,
  onChange,
  options,
  id,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  /** Optional: a stable id is generated when the caller has no reason to name one. */
  id?: string;
  disabled?: boolean;
}) {
  const generated = useId();
  const controlId = id ?? generated;
  return (
    <span
      style={{
        display: "inline-flex",
        flexDirection: "column",
        gap: "0.125rem",
      }}
    >
      <label
        htmlFor={controlId}
        style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}
      >
        {label}
      </label>
      <select
        id={controlId}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        style={{
          opacity: disabled ? 0.55 : 1,
          padding: "0.3125rem 0.5rem",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border-strong)",
          background: "var(--bg-raised)",
          color: "var(--text)",
          fontSize: "0.8125rem",
          fontFamily: "inherit",
        }}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </span>
  );
}

export function TextInput({
  label,
  value,
  onChange,
  id,
  placeholder,
  type = "text",
  required,
  autoComplete,
  disabled,
  describedBy,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  id?: string;
  placeholder?: string;
  type?: string;
  required?: boolean;
  autoComplete?: string;
  disabled?: boolean;
  describedBy?: string;
}) {
  const generated = useId();
  const controlId = id ?? generated;
  return (
    <span
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "0.125rem",
        flex: 1,
      }}
    >
      <label
        htmlFor={controlId}
        style={{ fontSize: "0.6875rem", color: "var(--text-muted)" }}
      >
        {label}
      </label>
      <input
        id={controlId}
        type={type}
        value={value}
        required={required}
        disabled={disabled}
        aria-describedby={describedBy}
        autoComplete={autoComplete}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        style={{
          padding: "0.375rem 0.5rem",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border-strong)",
          background: "var(--bg-raised)",
          color: "var(--text)",
          fontSize: "0.8125rem",
          fontFamily: "inherit",
          width: "100%",
        }}
      />
    </span>
  );
}

// ---------------------------------------------------------------------------
// data display
// ---------------------------------------------------------------------------

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  align?: "left" | "right";
  width?: string;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  caption,
  emptyMessage = "No results",
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  caption: string;
  emptyMessage?: string;
}) {
  if (rows.length === 0) {
    return <EmptyState title={emptyMessage} />;
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.8125rem",
        }}
      >
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                style={{
                  textAlign: column.align ?? "left",
                  padding: "0.5rem 0.75rem",
                  borderBottom: "1px solid var(--border)",
                  color: "var(--text-muted)",
                  fontWeight: 600,
                  fontSize: "0.75rem",
                  whiteSpace: "nowrap",
                  width: column.width,
                }}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={
                onRowClick
                  ? (event) => {
                      // Rows are activated by keyboard as well as by pointer;
                      // a click-only row is invisible to keyboard users.
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
              tabIndex={onRowClick ? 0 : undefined}
              role={onRowClick ? "button" : undefined}
              style={{
                cursor: onRowClick ? "pointer" : undefined,
                borderBottom: "1px solid var(--border)",
              }}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  style={{
                    padding: "0.5rem 0.75rem",
                    textAlign: column.align ?? "left",
                    verticalAlign: "top",
                  }}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * A single headline number.
 *
 * `invertDelta` exists because "up" is good for throughput and bad for error
 * rate. Without it every dashboard ends up encoding that judgement in an ad-hoc
 * colour choice at the call site, and they disagree with each other.
 */
export function Stat({
  label,
  value,
  hint,
  detail,
  previous,
  delta,
  invertDelta = false,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  detail?: ReactNode;
  /** Formatted previous-period value, shown alongside the change. */
  previous?: string;
  delta?: { absolute: number | null; relative: number | null };
  invertDelta?: boolean;
}) {
  const described = delta
    ? describeDelta(delta.absolute, delta.relative)
    : undefined;
  const arrow =
    described?.direction === "up"
      ? "▲"
      : described?.direction === "down"
        ? "▼"
        : "";
  const good =
    described === undefined || described.direction === "flat"
      ? null
      : (described.direction === "up") !== invertDelta;
  return (
    <div
      style={{
        padding: "0.875rem 1rem",
        background: "var(--bg-raised)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)",
      }}
    >
      <p style={{ margin: 0, fontSize: "0.75rem", color: "var(--text-muted)" }}>
        {label}
      </p>
      <p
        style={{
          margin: "0.25rem 0 0",
          fontSize: "1.5rem",
          fontWeight: 600,
          lineHeight: 1.1,
        }}
      >
        {value}
      </p>
      {described && (
        // The word as well as the arrow: direction must survive without colour.
        <p
          style={{
            margin: "0.25rem 0 0",
            fontSize: "0.75rem",
            color:
              good === null
                ? "var(--text-muted)"
                : good
                  ? "var(--ok)"
                  : "var(--warn)",
          }}
        >
          <span aria-hidden="true">{arrow} </span>
          {described.label} vs previous period
          {previous !== undefined && (
            <span style={{ color: "var(--text-faint)" }}>
              {" "}
              (was {previous})
            </span>
          )}
        </p>
      )}
      {!described && previous !== undefined && (
        <p
          style={{
            margin: "0.25rem 0 0",
            fontSize: "0.75rem",
            color: "var(--text-faint)",
          }}
        >
          previous period: {previous}
        </p>
      )}
      {detail && (
        <p
          style={{
            margin: "0.25rem 0 0",
            fontSize: "0.75rem",
            color: "var(--text-muted)",
          }}
        >
          {detail}
        </p>
      )}
      {hint && (
        <p
          style={{
            margin: "0.25rem 0 0",
            fontSize: "0.75rem",
            color: "var(--text-faint)",
          }}
        >
          {hint}
        </p>
      )}
    </div>
  );
}

/**
 * Render untrusted text.
 *
 * Prompts, completions, tool outputs and retrieved documents are all
 * attacker-influenced. React escapes by default, so the guarantee here is
 * simply that this component never reaches for `dangerouslySetInnerHTML` --
 * and its existence makes that a reviewable decision rather than an accident.
 */
export function SafeText({
  children,
  maxLines,
}: {
  children: string | null | undefined;
  maxLines?: number;
}) {
  if (!children) return <span style={{ color: "var(--text-faint)" }}>—</span>;
  return (
    <pre
      style={{
        margin: 0,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        fontFamily: "var(--mono)",
        fontSize: "0.75rem",
        lineHeight: 1.5,
        maxHeight: maxLines ? `${maxLines * 1.5}em` : undefined,
        overflow: maxLines ? "auto" : undefined,
      }}
    >
      {children}
    </pre>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(9rem, auto) 1fr",
        gap: "0.25rem 1rem",
        margin: 0,
        fontSize: "0.8125rem",
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

export const SERIES_COLOURS = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
  "var(--series-7)",
  "var(--series-8)",
];
