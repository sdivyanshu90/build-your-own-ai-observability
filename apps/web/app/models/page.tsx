"use client";

/**
 * Model registry.
 *
 * A "model" is provider + identifier; a *configuration version* is the hash of
 * the parameters that change behaviour (temperature, top_p, max tokens, stop
 * sequences, system fingerprint, deployment). Two runs with the same model name
 * and different temperatures are not the same thing, and this screen refuses to
 * pretend otherwise.
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
  Tag,
} from "@/components/ui";
import { formatTimestamp } from "@/lib/format";

interface ModelRow {
  id: string;
  provider: string;
  model_identifier: string;
  family: string | null;
  endpoint_kind: string;
}

export default function ModelsPage() {
  const workspace = useWorkspace();
  const [modelId, setModelId] = useState<string>("");

  const models = useQuery({
    queryKey: ["models", workspace.projectId],
    enabled: Boolean(workspace.projectId),
    queryFn: () => api.models(workspace.projectId ?? undefined),
  });

  useEffect(() => {
    if (!modelId && models.data && models.data.length > 0)
      setModelId(models.data[0]!.id);
  }, [models.data, modelId]);

  const versions = useQuery({
    queryKey: ["model-versions", modelId],
    enabled: Boolean(modelId),
    queryFn: () => api.modelVersions(modelId),
  });

  const columns: Column<ModelRow>[] = [
    {
      key: "model",
      header: "Model",
      render: (row) => (
        <button
          type="button"
          className="link-button"
          onClick={() => setModelId(row.id)}
          style={{ fontWeight: row.id === modelId ? 700 : 500 }}
        >
          {row.model_identifier}
        </button>
      ),
    },
    { key: "provider", header: "Provider", render: (row) => row.provider },
    { key: "family", header: "Family", render: (row) => row.family || "—" },
    {
      key: "endpoint",
      header: "Endpoint",
      render: (row) => <Tag>{row.endpoint_kind}</Tag>,
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Models</h1>
          <p>
            Registered models and their immutable configuration versions. Traces
            reference a configuration id, so a behaviour change caused by a
            parameter tweak is attributable.
          </p>
        </div>
      </header>

      {models.isLoading && <Loading label="Loading models" />}
      {models.isError && (
        <ErrorState error={models.error} onRetry={() => models.refetch()} />
      )}
      {models.data && models.data.length === 0 && (
        <EmptyState
          title="No models registered"
          description="Register a model with the SDK or API. Until then, spans still record a model name, but there is no configuration lineage to compare against."
        />
      )}

      {models.data && models.data.length > 0 && (
        <div
          style={{
            display: "grid",
            gap: "1rem",
            gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
          }}
        >
          <Card title="Registered models" padded={false}>
            <DataTable
              columns={columns}
              rows={models.data as ModelRow[]}
              rowKey={(row) => row.id}
              caption="Models registered in this organization"
            />
          </Card>

          <Card
            title="Configuration versions"
            subtitle="immutable; identified by parameter hash"
          >
            {versions.isLoading && <Loading />}
            {versions.isError && (
              <ErrorState
                error={versions.error}
                onRetry={() => versions.refetch()}
              />
            )}
            {versions.data && versions.data.length === 0 && (
              <p style={{ margin: 0, color: "var(--text-muted)" }}>
                No configuration versions recorded for this model.
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
                          "Config id",
                          <Mono key="i">{String(version.id ?? "—")}</Mono>,
                        ],
                        [
                          "Hash",
                          <Mono key="h">
                            {String(version.content_hash ?? "—").slice(0, 24)}…
                          </Mono>,
                        ],
                        ["Temperature", formatParameter(version.temperature)],
                        ["Top p", formatParameter(version.top_p)],
                        ["Max tokens", formatParameter(version.max_tokens)],
                        ["Stop sequences", formatList(version.stop_sequences)],
                        ["Deployment", String(version.deployment_name ?? "—")],
                        ["Region", String(version.region ?? "—")],
                        ["API version", String(version.api_version ?? "—")],
                        [
                          "System fingerprint",
                          version.system_fingerprint ? (
                            <Mono key="f">
                              {String(version.system_fingerprint)}
                            </Mono>
                          ) : (
                            "not reported by the provider"
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

/** `null` means the caller did not set the parameter, which is different from
 *  setting it to zero. Both must remain distinguishable. */
function formatParameter(value: unknown): string {
  if (value === null || value === undefined) return "provider default";
  return String(value);
}

function formatList(value: unknown): string {
  if (!Array.isArray(value) || value.length === 0) return "none";
  return value.map((item) => JSON.stringify(item)).join(", ");
}
