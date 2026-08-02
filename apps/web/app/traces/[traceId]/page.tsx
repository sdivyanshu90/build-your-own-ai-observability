"use client";

/**
 * Trace detail.
 *
 * The waterfall is the primary view; retrieval and trajectory are separate tabs
 * because they answer different questions and drawing them together produces a
 * picture nobody can read. The selected span and active tab are both in the URL
 * so a link points at a specific span, not just a trace.
 */

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { TraceDetail } from "@aiobs/schemas";

import { useWorkspace } from "../../providers";
import { api } from "@/lib/api";
import { AgentGraphView } from "@/components/AgentGraph";
import { RetrievalView } from "@/components/RetrievalView";
import { SpanDetail, Waterfall } from "@/components/Waterfall";
import {
  Card,
  ErrorState,
  KeyValue,
  Loading,
  PartialDataNotice,
  StatusBadge,
  Tag,
} from "@/components/ui";
import {
  formatCost,
  formatDuration,
  formatNumber,
  formatTimestamp,
} from "@/lib/format";

const TABS = [
  { key: "waterfall", label: "Waterfall" },
  { key: "retrieval", label: "Retrieval" },
  { key: "trajectory", label: "Agent trajectory" },
  { key: "metadata", label: "Metadata & lineage" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function TraceDetailPage() {
  return (
    <Suspense fallback={<Loading label="Loading trace" />}>
      <TraceDetailView />
    </Suspense>
  );
}

function TraceDetailView() {
  const workspace = useWorkspace();
  const router = useRouter();
  const params = useSearchParams();
  const route = useParams<{ traceId: string }>();
  const traceId = route.traceId;

  const tab = (params.get("tab") ?? "waterfall") as TabKey;
  const selectedSpanId = params.get("span");

  const setParam = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(params.toString());
    for (const [key, value] of Object.entries(updates)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    router.replace(`/traces/${traceId}?${next.toString()}`, { scroll: false });
  };

  const detail = useQuery({
    queryKey: ["trace", traceId, workspace.projectId, workspace.environment],
    enabled: Boolean(workspace.projectId && traceId),
    queryFn: () =>
      api.trace(traceId, {
        project_id: workspace.projectId!,
        environment: workspace.environment,
      }),
  });

  const retrieval = useQuery({
    queryKey: ["retrieval", traceId, workspace.projectId],
    enabled: Boolean(workspace.projectId && traceId) && tab === "retrieval",
    queryFn: () => api.retrieval(traceId, { project_id: workspace.projectId! }),
  });

  const trajectory = useQuery({
    queryKey: ["trajectory", traceId, workspace.projectId],
    enabled: Boolean(workspace.projectId && traceId) && tab === "trajectory",
    queryFn: () =>
      api.trajectory(traceId, { project_id: workspace.projectId! }),
  });

  const activeSpanId = useMemo(() => {
    if (selectedSpanId) return selectedSpanId;
    // Default to the root span rather than the first one returned: sort order
    // is by start time, and a late-arriving parent would otherwise win.
    const root = detail.data?.spans.find((span) => !span.parent_span_id);
    return root?.span_id ?? detail.data?.spans[0]?.span_id ?? null;
  }, [selectedSpanId, detail.data]);

  if (detail.isLoading) return <Loading label="Loading trace" />;
  if (detail.isError)
    return <ErrorState error={detail.error} onRetry={() => detail.refetch()} />;
  if (!detail.data) return null;

  const {
    trace,
    spans,
    orphan_span_ids: orphans,
    services,
    retry_groups: retryGroups,
  } = detail.data;

  return (
    <>
      <header className="page-header">
        <div>
          <h1 style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <StatusBadge status={trace.status} />
            {trace.name || "unnamed trace"}
          </h1>
          <p style={{ fontFamily: "var(--mono)", fontSize: "0.75rem" }}>
            {trace.trace_id}
          </p>
        </div>
        <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
          <Link href="/traces">← Back to traces</Link>
          <Link href={`/traces/compare?left=${trace.trace_id}`}>Compare →</Link>
        </div>
      </header>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(9rem, 1fr))",
          gap: "0.75rem",
          marginBottom: "1rem",
        }}
      >
        <Metric label="Duration" value={formatDuration(trace.duration_ms)} />
        <Metric
          label="Time to first token"
          value={formatDuration(trace.time_to_first_token_ms)}
          note={
            trace.time_to_first_token_ms === null ? "not streamed" : undefined
          }
        />
        <Metric
          label="Spans"
          value={formatNumber(trace.span_count)}
          note={`${services.length} service(s)`}
        />
        <Metric
          label="Tokens"
          value={formatNumber(trace.total_tokens)}
          note={`${trace.usage_source} counts`}
        />
        <Metric
          label="Cost"
          value={formatCost(trace.cost, trace.cost_currency)}
          note={trace.cost_status}
        />
        <Metric label="Errors" value={formatNumber(trace.error_count)} />
      </div>

      {!trace.complete && (
        <PartialDataNotice reason="This trace is incomplete: either it is still running, or spans were dropped or have not arrived yet. Durations and totals are lower bounds." />
      )}
      {orphans.length > 0 && (
        <PartialDataNotice
          reason={`${orphans.length} span(s) reference a parent that is not in this trace. They are shown at the root of the waterfall and marked. This usually means the parent was sampled out or dropped.`}
        />
      )}
      {Object.keys(retryGroups).length > 0 && (
        <PartialDataNotice
          reason={`${Object.keys(retryGroups).length} operation(s) were retried. Retried attempts are grouped in the waterfall; their durations are additive, not overlapping.`}
        />
      )}

      <div className="tabs" role="tablist" aria-label="Trace views">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            className="tab"
            aria-selected={tab === item.key}
            onClick={() => setParam({ tab: item.key })}
          >
            {item.label}
            {item.key === "retrieval" &&
              trace.retrieval_count > 0 &&
              ` (${trace.retrieval_count})`}
            {item.key === "trajectory" &&
              trace.agent_step_count > 0 &&
              ` (${trace.agent_step_count})`}
          </button>
        ))}
      </div>

      {tab === "waterfall" && (
        <div
          style={{
            display: "grid",
            gap: "1rem",
            gridTemplateColumns: "minmax(0, 3fr) minmax(0, 2fr)",
          }}
        >
          <Card
            title="Spans"
            subtitle={`${spans.length} span(s), critical path highlighted`}
            padded={false}
          >
            <Waterfall
              detail={detail.data}
              selectedSpanId={activeSpanId}
              onSelect={(spanId) => setParam({ span: spanId })}
            />
          </Card>
          <Card title="Span detail">
            {activeSpanId ? (
              <SpanDetail detail={detail.data} spanId={activeSpanId} />
            ) : (
              <p style={{ margin: 0, color: "var(--text-muted)" }}>
                Select a span.
              </p>
            )}
          </Card>
        </div>
      )}

      {tab === "retrieval" && (
        <>
          {retrieval.isLoading && <Loading label="Loading retrieval steps" />}
          {retrieval.isError && (
            <ErrorState
              error={retrieval.error}
              onRetry={() => retrieval.refetch()}
            />
          )}
          {retrieval.data && <RetrievalView stages={retrieval.data} />}
        </>
      )}

      {tab === "trajectory" && (
        <>
          {trajectory.isLoading && <Loading label="Loading trajectory" />}
          {trajectory.isError && (
            <ErrorState
              error={trajectory.error}
              onRetry={() => trajectory.refetch()}
            />
          )}
          {trajectory.data && <AgentGraphView graph={trajectory.data.graph} />}
        </>
      )}

      {tab === "metadata" && <Metadata detail={detail.data} />}
    </>
  );
}

function Metadata({ detail }: { detail: TraceDetail }) {
  const { trace, services } = detail;
  return (
    <div
      style={{
        display: "grid",
        gap: "1rem",
        gridTemplateColumns: "repeat(auto-fit, minmax(20rem, 1fr))",
      }}
    >
      <Card title="Request">
        <KeyValue
          items={[
            ["Trace id", <code key="t">{trace.trace_id}</code>],
            ["Environment", trace.environment],
            ["Release", trace.release || "—"],
            ["Started", formatTimestamp(trace.start_time)],
            ["Ended", formatTimestamp(trace.end_time)],
            ["Session", trace.session_id || "—"],
            ["Subject", trace.subject_id || "—"],
            [
              "Tags",
              trace.tags.length ? (
                <span
                  style={{ display: "flex", gap: "0.25rem", flexWrap: "wrap" }}
                >
                  {trace.tags.map((tag) => (
                    <Tag key={tag}>{tag}</Tag>
                  ))}
                </span>
              ) : (
                "—"
              ),
            ],
            ["Services", services.join(", ") || "—"],
          ]}
        />
      </Card>

      <Card
        title="Lineage"
        subtitle="exactly which versions produced this response"
      >
        <KeyValue
          items={[
            ["Models", trace.models.join(", ") || "—"],
            ["Providers", trace.providers.join(", ") || "—"],
            [
              "Prompt versions",
              trace.prompt_version_ids.length ? (
                <span style={{ display: "grid", gap: "0.125rem" }}>
                  {trace.prompt_version_ids.map((id) => (
                    <Link
                      key={id}
                      href={`/prompts?version=${id}`}
                      style={{ fontFamily: "var(--mono)", fontSize: "0.75rem" }}
                    >
                      {id}
                    </Link>
                  ))}
                </span>
              ) : (
                "none recorded"
              ),
            ],
            [
              "Model configs",
              trace.model_config_ids.join(", ") || "none recorded",
            ],
            [
              "Dataset versions",
              trace.dataset_version_ids.join(", ") || "none recorded",
            ],
          ]}
        />
        <p
          style={{
            margin: "0.75rem 0 0",
            fontSize: "0.75rem",
            color: "var(--text-muted)",
          }}
        >
          Missing lineage is not an error — it means the application did not
          record it. Without it, a regression cannot be attributed to a prompt
          or model change.
        </p>
      </Card>

      <Card title="Usage and cost">
        <KeyValue
          items={[
            ["Input tokens", formatNumber(trace.input_tokens)],
            ["Output tokens", formatNumber(trace.output_tokens)],
            ["Cached input tokens", formatNumber(trace.cached_input_tokens)],
            ["Token source", trace.usage_source],
            ["Cost", formatCost(trace.cost, trace.cost_currency)],
            ["Cost status", trace.cost_status],
            ["LLM calls", formatNumber(trace.llm_call_count)],
            ["Retrievals", formatNumber(trace.retrieval_count)],
            ["Tool calls", formatNumber(trace.tool_call_count)],
            ["Agent steps", formatNumber(trace.agent_step_count)],
          ]}
        />
      </Card>
    </div>
  );
}

function Metric({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note?: string;
}) {
  return (
    <div
      style={{
        padding: "0.625rem 0.75rem",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        background: "var(--bg-raised)",
      }}
    >
      <p
        style={{ margin: 0, fontSize: "0.6875rem", color: "var(--text-muted)" }}
      >
        {label}
      </p>
      <p
        style={{
          margin: "0.125rem 0 0",
          fontSize: "1.125rem",
          fontWeight: 600,
        }}
      >
        {value}
      </p>
      {note && (
        <p
          style={{
            margin: 0,
            fontSize: "0.6875rem",
            color: "var(--text-faint)",
          }}
        >
          {note}
        </p>
      )}
    </div>
  );
}
