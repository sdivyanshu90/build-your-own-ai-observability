"use client";

/**
 * Retrieval pipeline visualisation.
 *
 * Renders the stages a RAG request actually went through and, for each,
 * the evidence needed to judge it:
 *
 *     query → rewrite → embed → retrieve → rerank → select → generate
 *
 * The ranked list shows the pre- and post-rerank position of every document
 * together, because rank *movement* is the only way to tell whether the
 * reranker earned its latency. Documents that were retrieved but not selected
 * are shown too -- a high unused ratio is the most actionable retrieval signal
 * there is, and hiding them would make it invisible.
 */

import { useState } from "react";
import type { RetrievalStage } from "@aiobs/schemas";

import { formatDuration, formatNumber, formatPercent } from "@/lib/format";
import {
  Card,
  EmptyState,
  PartialDataNotice,
  SafeText,
  StatusBadge,
} from "./ui";

export function RetrievalView({ stages }: { stages: RetrievalStage[] }) {
  const [expanded, setExpanded] = useState<string | null>(
    stages[0]?.span_id ?? null,
  );

  if (stages.length === 0) {
    return (
      <EmptyState
        title="No retrieval in this trace"
        description="Retrieval steps appear here when a span records retrieved documents. Instrument them with span.record_retrieval() or the retrieval_span() helper."
      />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {stages.map((stage) => {
        const isOpen = expanded === stage.span_id;
        const diagnostics = stage.diagnostics;
        return (
          <Card
            key={stage.span_id}
            title={
              <span
                style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}
              >
                {stage.span_name}
                <span
                  style={{
                    fontWeight: 400,
                    color: "var(--text-muted)",
                    fontSize: "0.75rem",
                  }}
                >
                  {stage.retriever_name || "retriever"} ·{" "}
                  {formatDuration(stage.latency_ms)}
                </span>
              </span>
            }
            actions={
              <button
                type="button"
                aria-expanded={isOpen}
                onClick={() => setExpanded(isOpen ? null : stage.span_id)}
                style={{
                  border: "1px solid var(--border-strong)",
                  background: "var(--bg-raised)",
                  color: "var(--text)",
                  borderRadius: "var(--radius)",
                  padding: "0.25rem 0.5rem",
                  fontSize: "0.75rem",
                  cursor: "pointer",
                }}
              >
                {isOpen ? "Hide documents" : "Show documents"}
              </button>
            }
          >
            <Pipeline stages={stage.stages} />

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(9rem, 1fr))",
                gap: "0.75rem",
                marginTop: "1rem",
              }}
            >
              <Metric
                label="Retrieved"
                value={formatNumber(diagnostics.document_count)}
              />
              <Metric
                label="Selected"
                value={formatNumber(diagnostics.selected_count)}
              />
              <Metric
                label="Unused"
                value={formatPercent(diagnostics.unused_ratio)}
                hint={
                  diagnostics.unused_ratio > 0.6
                    ? "over-fetching, or context selection is dropping relevant material"
                    : undefined
                }
              />
              <Metric
                label="Context tokens"
                value={formatNumber(diagnostics.context_tokens)}
              />
              <Metric
                label="Rank movement"
                value={
                  diagnostics.mean_rank_movement === null
                    ? "not reranked"
                    : diagnostics.mean_rank_movement.toFixed(1)
                }
                hint={
                  diagnostics.reranked
                    ? `${diagnostics.rerank_promotions} up, ${diagnostics.rerank_demotions} down`
                    : undefined
                }
              />
              <Metric
                label="Score margin"
                value={
                  diagnostics.score_margin === null
                    ? "—"
                    : diagnostics.score_margin.toFixed(3)
                }
                hint={
                  diagnostics.score_margin !== null &&
                  diagnostics.score_margin < 0.02
                    ? "top results are nearly tied; ranking is close to arbitrary"
                    : undefined
                }
              />
            </div>

            {diagnostics.empty_result && (
              <PartialDataNotice reason="This retrieval returned no documents. The model answered without context." />
            )}
            {diagnostics.duplicate_document_ids.length > 0 && (
              <PartialDataNotice
                reason={`${diagnostics.duplicate_document_ids.length} document id(s) appear more than once: ${diagnostics.duplicate_document_ids.join(", ")}`}
              />
            )}
            {diagnostics.near_duplicate_pairs.length > 0 && (
              <PartialDataNotice
                reason={`${diagnostics.near_duplicate_pairs.length} near-duplicate chunk pair(s) detected — the context is spending tokens on repeated material. Detection compares truncated previews, so paraphrase is not caught.`}
              />
            )}
            {diagnostics.truncated_count > 0 && (
              <PartialDataNotice
                reason={`${diagnostics.truncated_count} document(s) were truncated to fit the context budget.`}
              />
            )}
            {diagnostics.missing_source_count > 0 && (
              <PartialDataNotice
                reason={`${diagnostics.missing_source_count} document(s) have no source, so answers citing them cannot be verified.`}
              />
            )}

            {isOpen && (
              <div style={{ marginTop: "1rem" }}>
                <Queries stage={stage} />
                <DocumentTable stage={stage} />
              </div>
            )}
          </Card>
        );
      })}
    </div>
  );
}

function Pipeline({ stages }: { stages: RetrievalStage["stages"] }) {
  return (
    <ol
      aria-label="Retrieval pipeline"
      style={{
        display: "flex",
        alignItems: "stretch",
        gap: "0.375rem",
        listStyle: "none",
        margin: 0,
        padding: 0,
        flexWrap: "wrap",
      }}
    >
      {stages.map((stage, index) => (
        <li
          key={stage.stage}
          style={{
            flex: "1 1 8rem",
            minWidth: "8rem",
            padding: "0.5rem 0.625rem",
            borderRadius: "var(--radius)",
            border: `1px solid ${stage.present ? "var(--border-strong)" : "var(--border)"}`,
            background: stage.present ? "var(--bg-subtle)" : "transparent",
            opacity: stage.present ? 1 : 0.45,
          }}
        >
          <p
            style={{
              margin: 0,
              fontSize: "0.6875rem",
              color: "var(--text-muted)",
            }}
          >
            {index + 1}. {stage.label}
          </p>
          <p
            style={{
              margin: "0.125rem 0 0",
              fontSize: "0.75rem",
              wordBreak: "break-word",
            }}
          >
            {stage.present ? stage.detail || "—" : "not used"}
          </p>
          {stage.latency_ms !== null && (
            <p
              style={{
                margin: "0.125rem 0 0",
                fontSize: "0.6875rem",
                color: "var(--text-faint)",
              }}
            >
              {formatDuration(stage.latency_ms)}
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}

function Queries({ stage }: { stage: RetrievalStage }) {
  return (
    <div style={{ marginBottom: "1rem", display: "grid", gap: "0.5rem" }}>
      <div>
        <h4 style={headingStyle}>Query</h4>
        <SafeText>{stage.query}</SafeText>
      </div>
      {stage.rewritten_query && stage.rewritten_query !== stage.query && (
        <div>
          <h4 style={headingStyle}>Rewritten query</h4>
          <SafeText>{stage.rewritten_query}</SafeText>
        </div>
      )}
    </div>
  );
}

function DocumentTable({ stage }: { stage: RetrievalStage }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.75rem",
        }}
      >
        <caption className="sr-only">
          Documents retrieved by {stage.span_name}, with rank before and after
          reranking
        </caption>
        <thead>
          <tr>
            {[
              "#",
              "After rerank",
              "Document",
              "Score",
              "Rerank",
              "Tokens",
              "In context",
              "Source",
            ].map((header) => (
              <th
                key={header}
                scope="col"
                style={{
                  textAlign: "left",
                  padding: "0.375rem 0.5rem",
                  borderBottom: "1px solid var(--border)",
                  color: "var(--text-muted)",
                  fontWeight: 600,
                  whiteSpace: "nowrap",
                }}
              >
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {stage.documents.map((document) => {
            const delta =
              document.rerank_rank === null ||
              document.rerank_rank === undefined
                ? null
                : document.rank - document.rerank_rank;
            return (
              <tr
                key={`${document.document_id}-${document.rank}`}
                style={{
                  borderBottom: "1px solid var(--border)",
                  background: document.selected
                    ? "var(--accent-subtle)"
                    : undefined,
                }}
              >
                <td style={cellStyle}>{document.rank}</td>
                <td style={cellStyle}>
                  {document.rerank_rank ?? "—"}
                  {delta !== null && delta !== 0 && (
                    // Both the arrow and the number: direction must not depend
                    // on colour perception.
                    <span
                      style={{
                        marginLeft: "0.25rem",
                        color: delta > 0 ? "var(--ok)" : "var(--warn)",
                      }}
                      title={
                        delta > 0 ? `promoted ${delta}` : `demoted ${-delta}`
                      }
                    >
                      {delta > 0 ? `▲${delta}` : `▼${-delta}`}
                    </span>
                  )}
                </td>
                <td style={{ ...cellStyle, maxWidth: "22rem" }}>
                  <div style={{ fontWeight: 500 }}>
                    {document.title || document.document_id}
                  </div>
                  {document.content && (
                    <div
                      style={{
                        color: "var(--text-muted)",
                        marginTop: "0.125rem",
                      }}
                    >
                      <SafeText maxLines={3}>{document.content}</SafeText>
                    </div>
                  )}
                  {!document.content && document.content_ref && (
                    <div
                      style={{
                        color: "var(--text-faint)",
                        fontSize: "0.6875rem",
                      }}
                    >
                      content stored externally
                    </div>
                  )}
                </td>
                <td style={cellStyle}>{document.score?.toFixed(4) ?? "—"}</td>
                <td style={cellStyle}>
                  {document.rerank_score?.toFixed(4) ?? "—"}
                </td>
                <td style={cellStyle}>
                  {formatNumber(document.token_count ?? null)}
                </td>
                <td style={cellStyle}>
                  {document.selected ? (
                    <StatusBadge status="ok" title="reached the model" />
                  ) : (
                    <span style={{ color: "var(--text-faint)" }}>no</span>
                  )}
                </td>
                <td
                  style={{
                    ...cellStyle,
                    maxWidth: "12rem",
                    wordBreak: "break-all",
                  }}
                >
                  {document.source ? (
                    <span style={{ color: "var(--text-muted)" }}>
                      {document.source}
                    </span>
                  ) : (
                    <span style={{ color: "var(--warn)" }}>missing</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div>
      <p
        style={{ margin: 0, fontSize: "0.6875rem", color: "var(--text-muted)" }}
      >
        {label}
      </p>
      <p style={{ margin: "0.125rem 0 0", fontSize: "1rem", fontWeight: 600 }}>
        {value}
      </p>
      {hint && (
        <p
          style={{
            margin: "0.125rem 0 0",
            fontSize: "0.6875rem",
            color: "var(--warn)",
          }}
        >
          {hint}
        </p>
      )}
    </div>
  );
}

const cellStyle: React.CSSProperties = {
  padding: "0.375rem 0.5rem",
  verticalAlign: "top",
};
const headingStyle: React.CSSProperties = {
  margin: "0 0 0.25rem",
  fontSize: "0.6875rem",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  color: "var(--text-muted)",
};
