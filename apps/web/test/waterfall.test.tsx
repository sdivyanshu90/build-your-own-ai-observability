import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SpanDetail, Waterfall } from "@/components/Waterfall";
import { makeSpan, makeTraceDetail } from "./fixtures";

describe("Waterfall", () => {
  it("renders the span tree with correct nesting levels", () => {
    render(
      <Waterfall
        detail={makeTraceDetail()}
        selectedSpanId={null}
        onSelect={() => {}}
      />,
    );

    const tree = screen.getByRole("tree", { name: "Span waterfall" });
    const items = within(tree).getAllByRole("treeitem");

    expect(items).toHaveLength(3);
    expect(items[0]).toHaveAttribute("aria-level", "1");
    expect(items[1]).toHaveAttribute("aria-level", "2");
    expect(items[2]).toHaveAttribute("aria-level", "2");
  });

  it("orders children under their parent rather than by arrival", () => {
    render(
      <Waterfall
        detail={makeTraceDetail()}
        selectedSpanId={null}
        onSelect={() => {}}
      />,
    );
    const names = within(screen.getByRole("tree"))
      .getAllByRole("treeitem")
      .map((item) => item.textContent);
    expect(names[0]).toContain("POST /chat");
    expect(names[1]).toContain("vector.search");
    expect(names[2]).toContain("openai.chat");
  });

  it("collapses a subtree and reports how many rows are hidden", () => {
    render(
      <Waterfall
        detail={makeTraceDetail()}
        selectedSpanId={null}
        onSelect={() => {}}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Collapse POST /chat" }),
    );

    const items = within(screen.getByRole("tree")).getAllByRole("treeitem");
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveTextContent("(+2)");
    expect(items[0]).toHaveAttribute("aria-expanded", "false");
  });

  it("reports selection through the callback rather than owning it", () => {
    const onSelect = vi.fn();
    render(
      <Waterfall
        detail={makeTraceDetail()}
        selectedSpanId="root"
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByTestId("waterfall-row-generate"));
    expect(onSelect).toHaveBeenCalledWith("generate");
    expect(screen.getByTestId("waterfall-row-root")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("is navigable by keyboard", () => {
    const onSelect = vi.fn();
    render(
      <Waterfall
        detail={makeTraceDetail()}
        selectedSpanId={null}
        onSelect={onSelect}
      />,
    );

    const first = screen.getByTestId("waterfall-row-root");
    fireEvent.keyDown(first, { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByTestId("waterfall-row-retrieve"), {
      key: "Enter",
    });

    expect(onSelect).toHaveBeenCalledWith("retrieve");
  });

  it("survives a trace whose spans all start at the same instant", () => {
    // Zero elapsed time must not divide by zero or produce NaN geometry.
    const instant = makeTraceDetail({
      spans: [
        makeSpan({
          span_id: "root",
          name: "instant",
          duration_ms: 0,
          end_time: "2026-03-01T10:00:00.000Z",
        }),
      ],
      children: { "": ["root"] },
      critical_path: ["root"],
    });
    render(
      <Waterfall detail={instant} selectedSpanId={null} onSelect={() => {}} />,
    );
    expect(screen.getAllByRole("treeitem")).toHaveLength(1);
    expect(document.body.innerHTML).not.toContain("NaN");
  });

  it("renders an empty trace without crashing", () => {
    const empty = makeTraceDetail({
      spans: [],
      children: {},
      critical_path: [],
    });
    render(
      <Waterfall detail={empty} selectedSpanId={null} onSelect={() => {}} />,
    );
    expect(screen.queryAllByRole("treeitem")).toHaveLength(0);
  });
});

describe("SpanDetail", () => {
  it("shows the auditable cost formula, not just a total", () => {
    render(<SpanDetail detail={makeTraceDetail()} spanId="generate" />);
    expect(screen.getByText(/\(100 \/ 1000000\) \* 2\.50/)).toBeInTheDocument();
  });

  it("renders money from the decimal string without float rounding", () => {
    render(<SpanDetail detail={makeTraceDetail()} spanId="generate" />);
    // 0.0012345 must not become $0.00.
    expect(screen.getByText("$0.0012")).toBeInTheDocument();
  });

  it("distinguishes a missing token count from zero", () => {
    render(<SpanDetail detail={makeTraceDetail()} spanId="retrieve" />);
    expect(screen.queryByText("0 in / 0 out")).not.toBeInTheDocument();
  });

  it("returns nothing for a span id that is not in the trace", () => {
    const { container } = render(
      <SpanDetail detail={makeTraceDetail()} spanId="does-not-exist" />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
