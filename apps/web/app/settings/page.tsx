"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { useWorkspace } from "../providers";
import { api } from "@/lib/api";
import {
  Card,
  ErrorState,
  KeyValue,
  Loading,
  StatusBadge,
} from "@/components/ui";

export default function SettingsOverviewPage() {
  const workspace = useWorkspace();

  const me = useQuery({ queryKey: ["me"], queryFn: () => api.me() });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(),
    retry: false,
  });

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Settings</h1>
          <p>Organization, project and platform configuration.</p>
        </div>
      </header>

      <div
        style={{
          display: "grid",
          gap: "1rem",
          gridTemplateColumns: "repeat(auto-fit, minmax(20rem, 1fr))",
        }}
      >
        <Card title="Session">
          {me.isLoading && <Loading />}
          {me.isError && (
            <ErrorState error={me.error} onRetry={() => me.refetch()} />
          )}
          {me.data && (
            <KeyValue
              items={[
                [
                  "Signed in as",
                  me.data.user.display_name || me.data.user.email,
                ],
                ["Email", me.data.user.email],
                ["Role", me.data.role],
              ]}
            />
          )}
          <p
            style={{
              margin: "0.75rem 0 0",
              fontSize: "0.75rem",
              color: "var(--text-muted)",
            }}
          >
            Your role determines what you can do here. Read-only roles see these
            screens but cannot change anything; the server enforces that
            regardless of what the UI shows.
          </p>
        </Card>

        <Card title="Project">
          <KeyValue
            items={[
              ["Name", workspace.project?.name ?? "—"],
              ["Slug", workspace.project?.slug ?? "—"],
              ["Description", workspace.project?.description ?? "—"],
              [
                "Default sampling rate",
                workspace.project
                  ? `${(workspace.project.default_sampling_rate * 100).toFixed(1)}%`
                  : "—",
              ],
              [
                "Environments",
                workspace.project?.environments
                  .map((environment) => environment.name)
                  .join(", ") ?? "—",
              ],
            ]}
          />
          <p style={{ margin: "0.75rem 0 0", fontSize: "0.75rem" }}>
            <Link href="/settings/retention">Configure retention →</Link>
          </p>
        </Card>

        <Card title="Platform health">
          {health.isLoading && <Loading />}
          {health.isError && (
            <ErrorState error={health.error} onRetry={() => health.refetch()} />
          )}
          {health.data && (
            <>
              <div style={{ marginBottom: "0.5rem" }}>
                <StatusBadge
                  status={health.data.status === "ok" ? "ok" : "error"}
                />
                <span
                  style={{
                    marginLeft: "0.5rem",
                    color: "var(--text-muted)",
                    fontSize: "0.75rem",
                  }}
                >
                  version {health.data.version}
                </span>
              </div>
              <KeyValue
                items={Object.entries(health.data.checks).map(
                  ([name, state]) => [name, state],
                )}
              />
            </>
          )}
        </Card>
      </div>
    </>
  );
}
