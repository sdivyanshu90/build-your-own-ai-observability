"use client";

/**
 * Price book viewer.
 *
 * Prices are effective-dated, so a trace from March is priced with March's
 * rates even after a provider changes them. That is the whole reason this is a
 * versioned table rather than a constants file, and the effective window is
 * therefore the most prominent column.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import {
  Card,
  Column,
  DataTable,
  ErrorState,
  Loading,
  Mono,
  Select,
  Tag,
} from "@/components/ui";
import { formatNumber, formatTimestamp } from "@/lib/format";

interface EntryRow {
  id: string;
  provider: string;
  model_identifier: string;
  usage_category: string;
  unit_quantity: number;
  unit_price: string;
  currency: string;
  effective_from: string;
  effective_to: string | null;
  source_url: string | null;
}

export default function PriceBooksPage() {
  const [bookId, setBookId] = useState("");

  const books = useQuery({
    queryKey: ["price-books"],
    queryFn: () => api.priceBooks(),
  });

  useEffect(() => {
    if (!bookId && books.data && books.data.length > 0)
      setBookId(books.data[0]!.id);
  }, [books.data, bookId]);

  const entries = useQuery({
    queryKey: ["price-entries", bookId],
    enabled: Boolean(bookId),
    queryFn: () => api.priceEntries(bookId),
  });

  const selected = books.data?.find((book) => book.id === bookId);

  const columns: Column<EntryRow>[] = [
    { key: "provider", header: "Provider", render: (row) => row.provider },
    {
      key: "model",
      header: "Model",
      render: (row) => <Mono>{row.model_identifier}</Mono>,
    },
    {
      key: "category",
      header: "Usage",
      render: (row) => <Tag>{row.usage_category}</Tag>,
    },
    {
      key: "price",
      header: "Price",
      align: "right",
      render: (row) => (
        // Rendered from the decimal string exactly as stored. Formatting it as
        // currency would round away the sub-cent precision that per-token
        // prices depend on.
        <Mono>
          {row.currency} {row.unit_price}
        </Mono>
      ),
    },
    {
      key: "unit",
      header: "Per",
      align: "right",
      render: (row) => `${formatNumber(row.unit_quantity)} units`,
    },
    {
      key: "effective",
      header: "Effective",
      render: (row) => (
        <span style={{ fontSize: "0.75rem" }}>
          {formatTimestamp(row.effective_from)}
          <br />
          <span style={{ color: "var(--text-muted)" }}>
            {row.effective_to
              ? `until ${formatTimestamp(row.effective_to)}`
              : "current"}
          </span>
        </span>
      ),
    },
    {
      key: "source",
      header: "Source",
      render: (row) =>
        row.source_url ? (
          <a href={row.source_url} target="_blank" rel="noreferrer noopener">
            provider page
          </a>
        ) : (
          <span style={{ color: "var(--warn)" }}>unsourced</span>
        ),
    },
  ];

  return (
    <>
      <header className="page-header">
        <div>
          <h1>Price books</h1>
          <p>
            Every cost the platform reports is traceable to one of these rows.
            Prices are effective-dated, so re-pricing a historical trace
            reproduces the original number.
          </p>
        </div>
        {books.data && books.data.length > 0 && (
          <Select
            label="Price book"
            value={bookId}
            onChange={setBookId}
            options={books.data.map((book) => ({
              value: book.id,
              label: `${book.name} ${book.version}${book.organization_id ? "" : " (built-in)"}`,
            }))}
          />
        )}
      </header>

      {books.isLoading && <Loading />}
      {books.isError && (
        <ErrorState error={books.error} onRetry={() => books.refetch()} />
      )}

      {selected && (
        <Card
          title={selected.name}
          subtitle={`version ${selected.version} · ${selected.currency}`}
        >
          <p
            style={{
              margin: 0,
              fontSize: "0.8125rem",
              color: "var(--text-muted)",
            }}
          >
            {selected.entry_count} entries · published{" "}
            {formatTimestamp(selected.published_at)} ·{" "}
            {selected.organization_id
              ? "organization-specific (overrides the built-in book)"
              : "built-in default, shared by all organizations"}
          </p>
        </Card>
      )}

      <div style={{ marginTop: "1rem" }}>
        <Card title="Entries" padded={false}>
          {entries.isLoading && <Loading />}
          {entries.isError && (
            <ErrorState
              error={entries.error}
              onRetry={() => entries.refetch()}
            />
          )}
          {entries.data && (
            <DataTable
              columns={columns}
              rows={entries.data as EntryRow[]}
              rowKey={(row) => row.id}
              caption="Price book entries"
              emptyMessage="This price book has no entries"
            />
          )}
        </Card>
      </div>
    </>
  );
}
