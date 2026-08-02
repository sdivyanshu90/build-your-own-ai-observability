"use client";

/**
 * Audit log viewer.
 *
 * Append-only by construction on the server. This screen exists so the log is
 * actually usable — an audit trail nobody can read is a compliance artefact,
 * not a control.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { useWorkspace } from "../../providers";
import { api } from "@/lib/api";
import {
  Button,
  Card,
  Column,
  DataTable,
  ErrorState,
  Loading,
  Mono,
  StatusBadge,
} from "@/components/ui";
import { formatTimestamp, timeWindow } from "@/lib/format";

interface AuditRow {
  id: string;
  occurred_at: string;
  action: string;
  actor_label: string | null;
  actor_type: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  request_id: string | null;
}

export default function AuditPage() {
  const workspace = useWorkspace();
  const [cursors, setCursors] = useState<string[]>([]);
  const window = useMemo(() => timeWindow(workspace.range), [workspace.range]);

  const cursor = cursors[cursors.length - 1];
  const events = useQuery({
    queryKey: ["audit", window.start, window.end, cursor ?? null],
    queryFn: () =>
      api.auditEvents({
        start: window.start,
        end: window.end,
        limit: 50,
        cursor,
      }),
  });

  const columns: Column<AuditRow>[] = [
    {
      key: "when",
      header: "When",
      render: (row) => formatTimestamp(row.occurred_at),
    },
    {
      key: "actor",
      header: "Actor",
      render: (row) => `${row.actor_label ?? "unknown"} (${row.actor_type})`,
    },
    {
      key: "action",
      header: "Action",
      render: (row) => <Mono>{row.action}</Mono>,
    },
    {
      key: "resource",
      header: "Resource",
      render: (row) => (
        <span>
          {row.resource_type}
          {row.resource_id && (
            <span style={{ color: "var(--text-muted)" }}>
              {" "}
              · {row.resource_id.slice(0, 16)}…
            </span>
          )}
        </span>
      ),
    },
    {
      key: "outcome",
      header: "Outcome",
      render: (row) => (
        <StatusBadge
          status={row.outcome === "success" ? "ok" : "error"}
          title={row.outcome}
        />
      ),
    },
    {
      key: "request",
      header: "Request id",
      render: (row) => (row.request_id ? <Mono>{row.request_id}</Mono> : "—"),
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Audit log</h1>
          <p>
            Every privileged action: authentication, key issuance and
            revocation, role changes, exports, retention changes and price book
            edits. Entries are immutable and carry the request id, so an entry
            can be correlated with the API logs that produced it.
          </p>
        </div>
      </header>

      <Card
        title="Events"
        subtitle={`window: last ${workspace.range}`}
        padded={false}
      >
        {events.isLoading && <Loading />}
        {events.isError && (
          <ErrorState error={events.error} onRetry={() => events.refetch()} />
        )}
        {events.data && (
          <>
            <DataTable
              columns={columns}
              rows={events.data.items as AuditRow[]}
              rowKey={(row) => row.id}
              caption="Audit events"
              emptyMessage="No audit events in this window"
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
              <Button
                disabled={!events.data.has_more || !events.data.next_cursor}
                onClick={() =>
                  setCursors((stack) =>
                    events.data?.next_cursor
                      ? [...stack, events.data.next_cursor]
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
    </>
  );
}
