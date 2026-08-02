"use client";

/**
 * API key management.
 *
 * The secret is displayed exactly once, at creation, and is never retrievable
 * afterwards — the server stores only a keyed hash. That is a hard property of
 * the backend, not a UI choice, and the screen says so plainly so nobody wastes
 * time looking for a "show key" button that cannot exist.
 */

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
  Mono,
  Select,
  TextInput,
} from "@/components/ui";
import { formatRelative, formatTimestamp } from "@/lib/format";

/**
 * The two scopes an API key may carry.
 *
 * Deliberately coarse, matching `ApiKeyScope` on the server: a key that can
 * administer the tenant is not an SDK credential, it is an account, and it
 * should go through a role instead.
 */
const SCOPES = [
  {
    value: "ingest",
    label: "Ingest",
    description:
      "Send telemetry, and read prompts and models so the SDK can resolve versions.",
  },
  {
    value: "read",
    label: "Read",
    description:
      "Read traces, spans, metrics, costs and registries for this project.",
  },
];

interface KeyRow {
  id: string;
  name: string;
  prefix: string;
  project_id: string;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export default function ApiKeysPage() {
  const workspace = useWorkspace();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [environmentId, setEnvironmentId] = useState("");
  const [scopes, setScopes] = useState<string[]>(["ingest"]);
  const [created, setCreated] = useState<{
    name: string;
    secret: string;
  } | null>(null);

  const environments = workspace.project?.environments ?? [];
  const resolvedEnvironmentId = environmentId || environments[0]?.id || "";

  const keys = useQuery({
    queryKey: ["api-keys", workspace.projectId],
    enabled: Boolean(workspace.projectId),
    queryFn: () => api.apiKeys(workspace.projectId ?? undefined),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createApiKey({
        name,
        project_id: workspace.projectId!,
        environment_id: resolvedEnvironmentId,
        scopes,
      }),
    onSuccess: (result) => {
      setCreated({ name: result.name, secret: result.secret });
      setName("");
      queryClient.invalidateQueries({ queryKey: ["api-keys"] });
    },
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeApiKey(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["api-keys"] }),
  });

  const columns: Column<KeyRow>[] = useMemo(
    () => [
      { key: "name", header: "Name", render: (row) => row.name },
      {
        key: "prefix",
        header: "Prefix",
        render: (row) => <Mono>{row.prefix}…</Mono>,
      },
      {
        key: "scopes",
        header: "Scopes",
        render: (row) => row.scopes.join(", ") || "none",
      },
      {
        key: "status",
        header: "Status",
        render: (row) =>
          row.revoked_at ? (
            <span style={{ color: "var(--error)" }}>
              revoked {formatRelative(row.revoked_at)}
            </span>
          ) : row.expires_at && new Date(row.expires_at) < new Date() ? (
            <span style={{ color: "var(--warn)" }}>expired</span>
          ) : (
            <span style={{ color: "var(--ok)" }}>active</span>
          ),
      },
      {
        key: "last_used",
        header: "Last used",
        render: (row) =>
          row.last_used_at ? formatRelative(row.last_used_at) : "never",
      },
      {
        key: "created",
        header: "Created",
        render: (row) => formatTimestamp(row.created_at),
      },
      {
        key: "actions",
        header: "",
        align: "right",
        render: (row) =>
          row.revoked_at ? null : (
            <Button
              variant="danger"
              disabled={revoke.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    `Revoke "${row.name}"? Anything using it will stop sending data immediately.`,
                  )
                ) {
                  revoke.mutate(row.id);
                }
              }}
            >
              Revoke
            </Button>
          ),
      },
    ],
    [revoke],
  );

  return (
    <>
      <header className="page-header">
        <div>
          <h1>API keys</h1>
          <p>
            Keys authenticate SDKs and the OTLP endpoint. The platform stores
            only a keyed hash, so a lost key cannot be recovered — issue a new
            one and revoke the old.
          </p>
        </div>
      </header>

      {created && (
        <div
          role="alert"
          style={{
            border: "1px solid var(--ok)",
            background: "var(--ok-subtle)",
            borderRadius: "var(--radius-lg)",
            padding: "1rem",
            marginBottom: "1rem",
          }}
        >
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.9375rem" }}>
            <span aria-hidden="true">✓ </span>
            Key “{created.name}” created
          </h2>
          <p style={{ margin: "0 0 0.5rem", fontSize: "0.8125rem" }}>
            Copy it now. This is the only time it will ever be shown.
          </p>
          <div
            style={{
              display: "flex",
              gap: "0.5rem",
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <code
              style={{
                fontFamily: "var(--mono)",
                fontSize: "0.8125rem",
                background: "var(--bg-raised)",
                border: "1px solid var(--border)",
                borderRadius: "var(--radius)",
                padding: "0.375rem 0.5rem",
                wordBreak: "break-all",
              }}
            >
              {created.secret}
            </code>
            <Button
              onClick={() => {
                void navigator.clipboard?.writeText(created.secret);
              }}
            >
              Copy
            </Button>
            <Button variant="ghost" onClick={() => setCreated(null)}>
              Dismiss
            </Button>
          </div>
        </div>
      )}

      <Card title="Create a key">
        <form
          style={{ display: "grid", gap: "0.75rem" }}
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <div
            style={{
              display: "flex",
              gap: "0.75rem",
              flexWrap: "wrap",
              alignItems: "flex-end",
            }}
          >
            <TextInput
              label="Name"
              value={name}
              onChange={setName}
              placeholder="checkout-service prod"
              required
            />
            <Select
              label="Environment"
              value={resolvedEnvironmentId}
              onChange={setEnvironmentId}
              options={environments.map((environment) => ({
                value: environment.id,
                label: environment.is_production
                  ? `${environment.name} (production)`
                  : environment.name,
              }))}
            />
          </div>

          <fieldset
            style={{
              border: "1px solid var(--border)",
              borderRadius: "var(--radius)",
              padding: "0.625rem",
            }}
          >
            <legend
              style={{
                fontSize: "0.75rem",
                color: "var(--text-muted)",
                padding: "0 0.25rem",
              }}
            >
              Scopes — grant only what the caller needs
            </legend>
            <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
              {SCOPES.map((scope) => (
                <label
                  key={scope.value}
                  style={{
                    display: "flex",
                    gap: "0.375rem",
                    fontSize: "0.8125rem",
                    maxWidth: "24rem",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={scopes.includes(scope.value)}
                    onChange={(event) =>
                      setScopes((current) =>
                        event.target.checked
                          ? [...current, scope.value]
                          : current.filter((item) => item !== scope.value),
                      )
                    }
                  />
                  <span>
                    <strong>{scope.label}</strong>
                    <br />
                    <span style={{ color: "var(--text-muted)" }}>
                      {scope.description}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {create.isError && <ErrorState error={create.error} />}

          <div>
            <Button
              type="submit"
              variant="primary"
              disabled={
                !name ||
                !resolvedEnvironmentId ||
                scopes.length === 0 ||
                create.isPending
              }
            >
              {create.isPending ? "Creating…" : "Create key"}
            </Button>
          </div>
        </form>
      </Card>

      <div style={{ marginTop: "1rem" }}>
        <Card title="Existing keys" padded={false}>
          {keys.isLoading && <Loading />}
          {keys.isError && (
            <ErrorState error={keys.error} onRetry={() => keys.refetch()} />
          )}
          {keys.data && (
            <DataTable
              columns={columns}
              rows={keys.data as KeyRow[]}
              rowKey={(row) => row.id}
              caption="API keys for this project"
              emptyMessage="No keys yet"
            />
          )}
        </Card>
      </div>
    </>
  );
}
