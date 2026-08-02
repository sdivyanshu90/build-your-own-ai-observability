"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useWorkspace } from "../../providers";
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
} from "@/components/ui";
import { formatNumber, formatTimestamp, timeWindow } from "@/lib/format";

interface ExportRow {
  id: string;
  resource: string;
  format: string;
  status: string;
  row_count: number;
  redacted: boolean;
  created_at: string;
}

export default function ExportsPage() {
  const workspace = useWorkspace();
  const queryClient = useQueryClient();
  const [resource, setResource] = useState("traces");
  const [format, setFormat] = useState("jsonl");
  const window = useMemo(() => timeWindow(workspace.range), [workspace.range]);

  const exports = useQuery({
    queryKey: ["exports"],
    queryFn: () => api.exports(),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createExport(
        { project_id: workspace.projectId!, resource, format },
        window,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["exports"] }),
  });

  const columns: Column<ExportRow>[] = [
    { key: "resource", header: "Resource", render: (row) => row.resource },
    { key: "format", header: "Format", render: (row) => row.format },
    {
      key: "status",
      header: "Status",
      render: (row) => (
        <StatusBadge
          status={
            row.status === "completed"
              ? "ok"
              : row.status === "failed"
                ? "error"
                : "unset"
          }
          title={row.status}
        />
      ),
    },
    {
      key: "rows",
      header: "Rows",
      align: "right",
      render: (row) => formatNumber(row.row_count),
    },
    {
      key: "redacted",
      header: "Redacted",
      render: (row) =>
        row.redacted ? (
          <span style={{ color: "var(--ok)" }}>sensitive fields removed</span>
        ) : (
          <span style={{ color: "var(--warn)" }}>raw — handle accordingly</span>
        ),
    },
    {
      key: "created",
      header: "Created",
      render: (row) => formatTimestamp(row.created_at),
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Exports</h1>
          <p>
            Exports are content-addressed: requesting the same window and
            resource twice returns the same object rather than duplicating it.
            Every export is recorded in the audit log with who requested it and
            what it contained.
          </p>
        </div>
      </header>

      <Card
        title="New export"
        subtitle={`window: ${window.start} → ${window.end} (from the range picker)`}
      >
        <form
          style={{
            display: "flex",
            gap: "0.75rem",
            alignItems: "flex-end",
            flexWrap: "wrap",
          }}
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <Select
            label="Resource"
            value={resource}
            onChange={setResource}
            options={[
              { value: "traces", label: "Traces" },
              { value: "spans", label: "Spans" },
              { value: "costs", label: "Cost records" },
            ]}
          />
          <Select
            label="Format"
            value={format}
            onChange={setFormat}
            options={[
              { value: "jsonl", label: "JSON Lines" },
              { value: "csv", label: "CSV" },
            ]}
          />
          <Button
            type="submit"
            variant="primary"
            disabled={!workspace.projectId || create.isPending}
          >
            {create.isPending ? "Queueing…" : "Create export"}
          </Button>
        </form>
        {create.isError && (
          <div style={{ marginTop: "0.75rem" }}>
            <ErrorState error={create.error} />
          </div>
        )}
      </Card>

      <div style={{ marginTop: "1rem" }}>
        <Card title="Recent exports" padded={false}>
          {exports.isLoading && <Loading />}
          {exports.isError && (
            <ErrorState
              error={exports.error}
              onRetry={() => exports.refetch()}
            />
          )}
          {exports.data && (
            <DataTable
              columns={columns}
              rows={exports.data as ExportRow[]}
              rowKey={(row) => row.id}
              caption="Export jobs"
              emptyMessage="No exports have been created"
            />
          )}
        </Card>
      </div>
    </>
  );
}
