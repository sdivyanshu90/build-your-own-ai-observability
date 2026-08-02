"use client";

/**
 * Dataset registry.
 *
 * Datasets carry obligations that prompts and models do not: a licence that
 * governs redistribution, and a flag for whether records contain personal data.
 * Both are surfaced prominently rather than buried in a metadata blob, because
 * "we did not know that evaluation set had PII in it" is not a defensible
 * position.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { useWorkspace } from "../providers";
import { api } from "@/lib/api";
import {
  Card,
  Column,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Mono,
} from "@/components/ui";
import { formatNumber, formatTimestamp } from "@/lib/format";

interface DatasetRow {
  id: string;
  name: string;
  description: string | null;
  license: string | null;
  contains_sensitive_data: boolean;
}

export default function DatasetsPage() {
  const workspace = useWorkspace();
  const [datasetId, setDatasetId] = useState("");

  const datasets = useQuery({
    queryKey: ["datasets", workspace.projectId],
    enabled: Boolean(workspace.projectId),
    queryFn: () => api.datasets(workspace.projectId!),
  });

  useEffect(() => {
    if (!datasetId && datasets.data && datasets.data.length > 0)
      setDatasetId(datasets.data[0]!.id);
  }, [datasets.data, datasetId]);

  const versions = useQuery({
    queryKey: ["dataset-versions", datasetId],
    enabled: Boolean(datasetId),
    queryFn: () => api.datasetVersions(datasetId),
  });

  const columns: Column<DatasetRow>[] = [
    {
      key: "name",
      header: "Dataset",
      render: (row) => (
        <button
          type="button"
          className="link-button"
          onClick={() => setDatasetId(row.id)}
          style={{ fontWeight: row.id === datasetId ? 700 : 500 }}
        >
          {row.name}
        </button>
      ),
    },
    {
      key: "license",
      header: "Licence",
      render: (row) => row.license || "not declared",
    },
    {
      key: "sensitive",
      header: "Personal data",
      render: (row) =>
        row.contains_sensitive_data ? (
          <span style={{ color: "var(--warn)" }}>
            <span aria-hidden="true">! </span>contains personal data
          </span>
        ) : (
          <span style={{ color: "var(--text-muted)" }}>none declared</span>
        ),
    },
    {
      key: "description",
      header: "Description",
      render: (row) => row.description || "—",
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Datasets</h1>
          <p>
            Evaluation and fine-tuning datasets, versioned by content hash and
            row count. A trace can reference the dataset version it was
            evaluated against.
          </p>
        </div>
      </header>

      {datasets.isLoading && <Loading label="Loading datasets" />}
      {datasets.isError && (
        <ErrorState error={datasets.error} onRetry={() => datasets.refetch()} />
      )}
      {datasets.data && datasets.data.length === 0 && (
        <EmptyState
          title="No datasets registered"
          description="Register a dataset to version evaluation sets alongside prompts and models."
        />
      )}

      {datasets.data && datasets.data.length > 0 && (
        <div style={{ display: "grid", gap: "1rem" }}>
          <Card title="Datasets" padded={false}>
            <DataTable
              columns={columns}
              rows={datasets.data as DatasetRow[]}
              rowKey={(row) => row.id}
              caption="Registered datasets"
            />
          </Card>

          <Card title="Versions" subtitle="each version is a fixed set of rows">
            {versions.isLoading && <Loading />}
            {versions.isError && (
              <ErrorState
                error={versions.error}
                onRetry={() => versions.refetch()}
              />
            )}
            {versions.data && versions.data.length === 0 && (
              <p style={{ margin: 0, color: "var(--text-muted)" }}>
                No versions recorded.
              </p>
            )}
            {versions.data && versions.data.length > 0 && (
              <ol
                style={{
                  margin: 0,
                  padding: 0,
                  listStyle: "none",
                  display: "grid",
                  gap: "0.75rem",
                }}
              >
                {versions.data.map((version, index) => (
                  <li
                    key={String(version.id ?? index)}
                    style={{
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius)",
                      padding: "0.625rem 0.75rem",
                    }}
                  >
                    <KeyValue
                      items={[
                        [
                          "Version",
                          String(version.version_number ?? index + 1),
                        ],
                        [
                          "Id",
                          <Mono key="i">{String(version.id ?? "—")}</Mono>,
                        ],
                        [
                          "Content hash",
                          <Mono key="h">
                            {String(version.content_hash ?? "—").slice(0, 24)}…
                          </Mono>,
                        ],
                        ["Rows", formatNumber(toNumber(version.row_count))],
                        ["Split", String(version.split ?? "not specified")],
                        [
                          "Storage",
                          String(
                            version.storage_uri ?? "not stored by the platform",
                          ),
                        ],
                        [
                          "Created",
                          formatTimestamp(String(version.created_at ?? "")),
                        ],
                      ]}
                    />
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>
      )}
    </>
  );
}

function toNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
