"use client";

import { useQuery } from "@tanstack/react-query";

import { useWorkspace } from "../../providers";
import { api } from "@/lib/api";
import { Card, ErrorState, KeyValue, Loading } from "@/components/ui";

export default function RetentionPage() {
  const workspace = useWorkspace();

  const policies = useQuery({
    queryKey: ["retention", workspace.projectId],
    enabled: Boolean(workspace.projectId),
    queryFn: () => api.retention(workspace.projectId!),
  });

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Retention</h1>
          <p>
            Three independent horizons, because the data has three different
            risk profiles. Payloads (prompts and completions) carry the most
            exposure and should expire first; aggregates carry almost none and
            are what dashboards read.
          </p>
        </div>
      </header>

      {policies.isLoading && <Loading />}
      {policies.isError && (
        <ErrorState error={policies.error} onRetry={() => policies.refetch()} />
      )}

      {policies.data && (
        <div
          style={{
            display: "grid",
            gap: "1rem",
            gridTemplateColumns: "repeat(auto-fit, minmax(20rem, 1fr))",
          }}
        >
          {policies.data.map((policy) => (
            <Card key={policy.id} title="Policy">
              <KeyValue
                items={[
                  ["Raw spans", `${policy.raw_span_days} days`],
                  ["Aggregates", `${policy.aggregate_days} days`],
                  ["Payloads", `${policy.payload_days} days`],
                ]}
              />
              <p
                style={{
                  margin: "0.75rem 0 0",
                  fontSize: "0.75rem",
                  color: "var(--text-muted)",
                }}
              >
                Deletion is performed by a sweep job in bounded batches. Objects
                in blob storage are removed only after the rows that reference
                them, so a partial sweep never leaves a trace pointing at a
                deleted payload.
              </p>
            </Card>
          ))}
          {policies.data.length === 0 && (
            <Card title="Policy">
              <p style={{ margin: 0, color: "var(--text-muted)" }}>
                No project-specific policy. The organization default applies.
              </p>
            </Card>
          )}
        </div>
      )}

      <div style={{ marginTop: "1rem" }}>
        <Card title="What deletion actually does">
          <ul
            style={{
              margin: 0,
              paddingLeft: "1.25rem",
              fontSize: "0.8125rem",
              display: "grid",
              gap: "0.375rem",
            }}
          >
            <li>
              <strong>Raw spans</strong> — the per-span rows behind the
              waterfall. Once these expire a trace can no longer be opened, but
              its aggregate contribution remains.
            </li>
            <li>
              <strong>Aggregates</strong> — pre-rolled counters and percentile
              states. Dashboards read these, so this horizon governs how far
              back charts go.
            </li>
            <li>
              <strong>Payloads</strong> — prompt and completion bodies in object
              storage. Expiring these first limits how long sensitive text is
              retained while keeping the shape of the request observable.
            </li>
            <li>
              A subject-deletion request removes payloads and subject
              identifiers for that subject immediately, independent of these
              horizons.
            </li>
          </ul>
        </Card>
      </div>
    </>
  );
}
