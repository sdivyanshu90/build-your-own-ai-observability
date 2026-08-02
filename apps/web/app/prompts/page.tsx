"use client";

/**
 * Prompt registry.
 *
 * Versions are content-addressed and immutable; aliases (`production`,
 * `staging`) move between them. That distinction is the whole point of the
 * screen, so it is made visually explicit: the alias is a label attached to a
 * hash, never a thing that can itself be edited.
 *
 * The diff view compares two immutable versions. Because ids are content
 * hashes, "identical" is a fact rather than a heuristic.
 */

import { Suspense, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { PromptVersion } from "@aiobs/schemas";

import { useWorkspace } from "../providers";
import { api } from "@/lib/api";
import {
  Card,
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Mono,
  SafeText,
  Select,
  Tag,
} from "@/components/ui";
import { formatTimestamp } from "@/lib/format";

export default function PromptsPage() {
  return (
    <Suspense fallback={<Loading label="Loading prompts" />}>
      <PromptRegistry />
    </Suspense>
  );
}

function PromptRegistry() {
  const workspace = useWorkspace();
  const [promptId, setPromptId] = useState<string>("");
  const [versionId, setVersionId] = useState<string>("");
  const [compareId, setCompareId] = useState<string>("");

  const prompts = useQuery({
    queryKey: ["prompts", workspace.projectId],
    enabled: Boolean(workspace.projectId),
    queryFn: () => api.prompts(workspace.projectId!),
  });

  useEffect(() => {
    if (!promptId && prompts.data && prompts.data.length > 0) {
      setPromptId(prompts.data[0]!.id);
    }
  }, [prompts.data, promptId]);

  const versions = useQuery({
    queryKey: ["prompt-versions", promptId],
    enabled: Boolean(promptId),
    queryFn: () => api.promptVersions(promptId),
  });

  const aliases = useQuery({
    queryKey: ["prompt-aliases", promptId],
    enabled: Boolean(promptId),
    queryFn: () => api.promptAliases(promptId),
  });

  useEffect(() => {
    if (versions.data && versions.data.length > 0) {
      const known = versions.data.some((version) => version.id === versionId);
      if (!known) {
        setVersionId(versions.data[0]!.id);
        setCompareId(versions.data[1]?.id ?? "");
      }
    }
  }, [versions.data, versionId]);

  const diff = useQuery({
    queryKey: ["prompt-diff", versionId, compareId],
    enabled: Boolean(versionId && compareId && versionId !== compareId),
    queryFn: () => api.promptDiff(versionId, compareId),
  });

  const selected =
    versions.data?.find((version) => version.id === versionId) ?? null;
  const aliasesForSelected = (aliases.data ?? []).filter(
    (alias) => alias.version_id === versionId,
  );

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Prompts</h1>
          <p>
            Every version is immutable and identified by the hash of its
            content. Aliases move; the versions they point at never change, so a
            trace recorded last month still resolves to exactly the text that
            produced it.
          </p>
        </div>
      </header>

      {prompts.isLoading && <Loading label="Loading prompts" />}
      {prompts.isError && (
        <ErrorState error={prompts.error} onRetry={() => prompts.refetch()} />
      )}
      {prompts.data && prompts.data.length === 0 && (
        <EmptyState
          title="No prompts registered"
          description="Register a prompt with the SDK (client.prompts.register) or the API to start tracking versions. Traces will then link to the exact text used."
        />
      )}

      {prompts.data && prompts.data.length > 0 && (
        <div
          style={{
            display: "grid",
            gap: "1rem",
            gridTemplateColumns: "minmax(0, 1fr) minmax(0, 2fr)",
          }}
        >
          <div style={{ display: "grid", gap: "1rem", alignContent: "start" }}>
            <Card title="Prompt">
              <Select
                label="Registered prompts"
                value={promptId}
                onChange={(value) => {
                  setPromptId(value);
                  setVersionId("");
                  setCompareId("");
                }}
                options={prompts.data.map((prompt) => ({
                  value: prompt.id,
                  label: prompt.name,
                }))}
              />
              {(() => {
                const prompt = prompts.data!.find(
                  (item) => item.id === promptId,
                );
                if (!prompt) return null;
                return (
                  <div style={{ marginTop: "0.75rem" }}>
                    <KeyValue
                      items={[
                        ["Description", prompt.description || "—"],
                        [
                          "Tags",
                          prompt.tags.length ? (
                            <span
                              style={{
                                display: "flex",
                                gap: "0.25rem",
                                flexWrap: "wrap",
                              }}
                            >
                              {prompt.tags.map((tag) => (
                                <Tag key={tag}>{tag}</Tag>
                              ))}
                            </span>
                          ) : (
                            "—"
                          ),
                        ],
                      ]}
                    />
                  </div>
                );
              })()}
            </Card>

            <Card
              title="Aliases"
              subtitle="movable pointers into the version history"
            >
              {aliases.isLoading && <Loading />}
              {aliases.data && aliases.data.length === 0 && (
                <p style={{ margin: 0, color: "var(--text-muted)" }}>
                  No aliases defined.
                </p>
              )}
              {aliases.data && aliases.data.length > 0 && (
                <ul
                  style={{
                    margin: 0,
                    padding: 0,
                    listStyle: "none",
                    display: "grid",
                    gap: "0.5rem",
                  }}
                >
                  {aliases.data.map((alias) => (
                    <li key={alias.name}>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => setVersionId(alias.version_id)}
                        style={{ textAlign: "left" }}
                      >
                        <strong>{alias.name}</strong>
                      </button>
                      <div
                        style={{
                          fontSize: "0.75rem",
                          color: "var(--text-muted)",
                        }}
                      >
                        <Mono>{alias.version_id.slice(0, 20)}…</Mono>
                        <br />
                        promoted {formatTimestamp(alias.promoted_at)}
                        {alias.previous_version_id && (
                          <>
                            {" "}
                            · rollback target{" "}
                            <Mono>
                              {alias.previous_version_id.slice(0, 12)}…
                            </Mono>
                          </>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            <Card
              title="Versions"
              subtitle={`${versions.data?.length ?? 0} immutable version(s)`}
            >
              {versions.isLoading && <Loading />}
              {versions.isError && (
                <ErrorState
                  error={versions.error}
                  onRetry={() => versions.refetch()}
                />
              )}
              {versions.data && (
                <ul
                  style={{
                    margin: 0,
                    padding: 0,
                    listStyle: "none",
                    display: "grid",
                    gap: "0.25rem",
                  }}
                >
                  {versions.data.map((version) => (
                    <li key={version.id}>
                      <button
                        type="button"
                        onClick={() => setVersionId(version.id)}
                        aria-current={
                          version.id === versionId ? "true" : undefined
                        }
                        style={{
                          width: "100%",
                          textAlign: "left",
                          border: "1px solid",
                          borderColor:
                            version.id === versionId
                              ? "var(--accent)"
                              : "var(--border)",
                          background:
                            version.id === versionId
                              ? "var(--accent-subtle)"
                              : "var(--bg-raised)",
                          borderRadius: "var(--radius)",
                          padding: "0.375rem 0.5rem",
                          cursor: "pointer",
                          font: "inherit",
                          color: "var(--text)",
                        }}
                      >
                        <strong>v{version.version_number}</strong>{" "}
                        <span
                          style={{
                            color: "var(--text-muted)",
                            fontSize: "0.75rem",
                          }}
                        >
                          {version.release_stage}
                        </span>
                        <div
                          style={{
                            fontSize: "0.6875rem",
                            color: "var(--text-faint)",
                          }}
                        >
                          {formatTimestamp(version.created_at)} ·{" "}
                          <Mono>{version.content_hash.slice(0, 12)}…</Mono>
                        </div>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </div>

          <div style={{ display: "grid", gap: "1rem", alignContent: "start" }}>
            {selected && (
              <VersionDetail
                version={selected}
                aliases={aliasesForSelected.map((a) => a.name)}
              />
            )}

            <Card
              title="Compare"
              subtitle="two immutable versions; identical content means identical hashes"
              actions={
                versions.data && versions.data.length > 1 ? (
                  <Select
                    label="Against"
                    value={compareId}
                    onChange={setCompareId}
                    options={[
                      { value: "", label: "Select a version" },
                      ...versions.data
                        .filter((version) => version.id !== versionId)
                        .map((version) => ({
                          value: version.id,
                          label: `v${version.version_number} (${version.content_hash.slice(0, 8)})`,
                        })),
                    ]}
                  />
                ) : undefined
              }
            >
              {!compareId && (
                <p style={{ margin: 0, color: "var(--text-muted)" }}>
                  Choose a version to diff against.
                </p>
              )}
              {diff.isLoading && <Loading />}
              {diff.isError && (
                <ErrorState error={diff.error} onRetry={() => diff.refetch()} />
              )}
              {diff.data && <Diff diff={diff.data} />}
            </Card>
          </div>
        </div>
      )}
    </>
  );
}

function VersionDetail({
  version,
  aliases,
}: {
  version: PromptVersion;
  aliases: string[];
}) {
  return (
    <Card
      title={`Version ${version.version_number}`}
      subtitle={version.label || version.release_stage}
      actions={
        aliases.length > 0 ? (
          <span style={{ display: "flex", gap: "0.25rem" }}>
            {aliases.map((alias) => (
              <Tag key={alias}>{alias}</Tag>
            ))}
          </span>
        ) : undefined
      }
    >
      <KeyValue
        items={[
          ["Content hash", <Mono key="h">{version.content_hash}</Mono>],
          ["Template engine", version.template_engine],
          ["Release stage", version.release_stage],
          ["Created", formatTimestamp(version.created_at)],
          [
            "Published",
            version.published_at
              ? formatTimestamp(version.published_at)
              : "not published",
          ],
          [
            "Parent",
            version.parent_version_id ? (
              <Mono key="p">{version.parent_version_id}</Mono>
            ) : (
              "—"
            ),
          ],
          ["Commit message", version.commit_message || "—"],
          [
            "Variables",
            Object.keys(version.variable_schema ?? {}).join(", ") ||
              "none declared",
          ],
        ]}
      />

      <h3 style={{ margin: "1rem 0 0.5rem", fontSize: "0.8125rem" }}>
        Messages
      </h3>
      <ol
        style={{
          margin: 0,
          padding: 0,
          listStyle: "none",
          display: "grid",
          gap: "0.5rem",
        }}
      >
        {version.messages.map((message, index) => (
          <li
            key={index}
            style={{
              border: "1px solid var(--border)",
              borderRadius: "var(--radius)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                padding: "0.25rem 0.5rem",
                background: "var(--bg-subtle)",
                fontSize: "0.6875rem",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
                color: "var(--text-muted)",
              }}
            >
              {message.role}
            </div>
            <div style={{ padding: "0.5rem" }}>
              <SafeText>{message.content}</SafeText>
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function Diff({ diff }: { diff: Awaited<ReturnType<typeof api.promptDiff>> }) {
  if (diff.identical) {
    return (
      <p style={{ margin: 0, color: "var(--ok)" }}>
        <span aria-hidden="true">✓ </span>
        Identical content. Both versions hash to{" "}
        <Mono>{diff.left_hash.slice(0, 16)}…</Mono>
      </p>
    );
  }
  return (
    <div style={{ display: "grid", gap: "0.75rem" }}>
      <KeyValue
        items={[
          ["Left hash", <Mono key="l">{diff.left_hash.slice(0, 24)}…</Mono>],
          ["Right hash", <Mono key="r">{diff.right_hash.slice(0, 24)}…</Mono>],
          [
            "Template engine",
            diff.engine_changed ? (
              <span style={{ color: "var(--warn)" }}>
                changed — rendering semantics differ
              </span>
            ) : (
              "unchanged"
            ),
          ],
        ]}
      />

      <div>
        <h4
          style={{
            margin: "0 0 0.25rem",
            fontSize: "0.75rem",
            color: "var(--text-muted)",
          }}
        >
          Variables
        </h4>
        <ul style={{ margin: 0, paddingLeft: "1rem", fontSize: "0.8125rem" }}>
          {diff.variable_changes.added.map((name) => (
            <li key={`add-${name}`} style={{ color: "var(--ok)" }}>
              <span aria-hidden="true">+ </span>added {name}
            </li>
          ))}
          {diff.variable_changes.removed.map((name) => (
            <li key={`rem-${name}`} style={{ color: "var(--error)" }}>
              <span aria-hidden="true">− </span>removed {name} — any caller
              still passing it will fail
            </li>
          ))}
          {diff.variable_changes.modified.map((name) => (
            <li key={`mod-${name}`} style={{ color: "var(--warn)" }}>
              <span aria-hidden="true">~ </span>changed {name}
            </li>
          ))}
          {diff.variable_changes.added.length === 0 &&
            diff.variable_changes.removed.length === 0 &&
            diff.variable_changes.modified.length === 0 && (
              <li style={{ color: "var(--text-muted)" }}>
                No variable changes.
              </li>
            )}
        </ul>
      </div>

      <div>
        <h4
          style={{
            margin: "0 0 0.25rem",
            fontSize: "0.75rem",
            color: "var(--text-muted)",
          }}
        >
          Messages
        </h4>
        <ol
          style={{
            margin: 0,
            paddingLeft: "1rem",
            fontSize: "0.8125rem",
            display: "grid",
            gap: "0.5rem",
          }}
        >
          {diff.message_changes.map((change, index) => {
            const before = asMessage(change.before);
            const after = asMessage(change.after);
            return (
              <li key={index}>
                <strong>
                  message {String(change.index ?? index)}{" "}
                  {String(change.change ?? "changed")}
                </strong>
                {change.role_changed === true && (
                  <span style={{ color: "var(--warn)" }}> · role changed</span>
                )}
                {before && (
                  <div style={{ marginTop: "0.25rem" }}>
                    <span
                      style={{
                        fontSize: "0.6875rem",
                        color: "var(--text-muted)",
                      }}
                    >
                      before ({before.role})
                    </span>
                    <SafeText maxLines={6}>{before.content}</SafeText>
                  </div>
                )}
                {after && (
                  <div style={{ marginTop: "0.25rem" }}>
                    <span
                      style={{
                        fontSize: "0.6875rem",
                        color: "var(--text-muted)",
                      }}
                    >
                      after ({after.role})
                    </span>
                    <SafeText maxLines={6}>{after.content}</SafeText>
                  </div>
                )}
              </li>
            );
          })}
          {diff.message_changes.length === 0 && (
            <li style={{ color: "var(--text-muted)" }}>No message changes.</li>
          )}
        </ol>
      </div>
    </div>
  );
}

/** Narrow an untyped diff payload entry to a prompt message. */
function asMessage(value: unknown): { role: string; content: string } | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  return {
    role: typeof record.role === "string" ? record.role : "unknown",
    content:
      typeof record.content === "string"
        ? record.content
        : JSON.stringify(record.content ?? ""),
  };
}
