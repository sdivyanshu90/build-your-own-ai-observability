import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RetrievalView } from "@/components/RetrievalView";
import { makeRetrievalStage } from "./fixtures";

describe("RetrievalView", () => {
  it("explains what to do when a trace has no retrieval at all", () => {
    render(<RetrievalView stages={[]} />);
    expect(screen.getByText("No retrieval in this trace")).toBeInTheDocument();
    expect(screen.getByText(/record_retrieval/)).toBeInTheDocument();
  });

  it("renders the pipeline stages in order", () => {
    render(<RetrievalView stages={[makeRetrievalStage()]} />);
    const pipeline = screen.getByRole("list", { name: "Retrieval pipeline" });
    expect(pipeline.textContent).toContain("1. Query");
    expect(pipeline.textContent).toContain("5. Rerank");
    expect(pipeline.textContent).toContain("7. Generate");
  });

  it("shows both the pre- and post-rerank rank so movement is visible", () => {
    render(<RetrievalView stages={[makeRetrievalStage()]} />);
    // doc-2 moved from rank 2 to rank 1: promoted by one place.
    expect(screen.getByTitle("promoted 1")).toHaveTextContent("▲1");
    expect(screen.getByTitle("demoted 1")).toHaveTextContent("▼1");
  });

  it("lists retrieved-but-unused documents rather than hiding them", () => {
    render(<RetrievalView stages={[makeRetrievalStage()]} />);
    expect(screen.getByText("Shipping")).toBeInTheDocument();
    expect(screen.getByText("33.3%")).toBeInTheDocument();
  });

  it("flags a document with no source as unverifiable", () => {
    render(<RetrievalView stages={[makeRetrievalStage()]} />);
    expect(screen.getByText("missing")).toBeInTheDocument();
    expect(
      screen.getByText(/answers citing them cannot be verified/),
    ).toBeInTheDocument();
  });

  it("warns when the top scores are nearly tied", () => {
    render(<RetrievalView stages={[makeRetrievalStage()]} />);
    expect(
      screen.getByText(/ranking is close to arbitrary/),
    ).toBeInTheDocument();
  });

  it("warns loudly when retrieval returned nothing", () => {
    const stage = makeRetrievalStage();
    render(
      <RetrievalView
        stages={[
          {
            ...stage,
            documents: [],
            diagnostics: {
              ...stage.diagnostics,
              empty_result: true,
              document_count: 0,
              selected_count: 0,
            },
          },
        ]}
      />,
    );
    expect(screen.getByText(/answered without context/)).toBeInTheDocument();
  });

  it("reports duplicate and near-duplicate chunks", () => {
    const stage = makeRetrievalStage();
    render(
      <RetrievalView
        stages={[
          {
            ...stage,
            diagnostics: {
              ...stage.diagnostics,
              duplicate_document_ids: ["doc-1"],
              near_duplicate_pairs: [["doc-1", "doc-2"]],
            },
          },
        ]}
      />,
    );
    expect(screen.getByText(/appear more than once/)).toBeInTheDocument();
    expect(screen.getByText(/near-duplicate chunk pair/)).toBeInTheDocument();
  });

  it('says "not reranked" instead of showing a zero movement', () => {
    const stage = makeRetrievalStage();
    render(
      <RetrievalView
        stages={[
          {
            ...stage,
            diagnostics: {
              ...stage.diagnostics,
              reranked: false,
              mean_rank_movement: null,
            },
          },
        ]}
      />,
    );
    expect(screen.getByText("not reranked")).toBeInTheDocument();
  });

  it("can hide and show the document table", () => {
    render(<RetrievalView stages={[makeRetrievalStage()]} />);
    const toggle = screen.getByRole("button", { name: "Hide documents" });
    expect(screen.getByText("Refund policy")).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.queryByText("Refund policy")).not.toBeInTheDocument();
  });
});
