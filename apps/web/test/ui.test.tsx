import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  DataTable,
  EmptyState,
  ErrorState,
  Loading,
  PartialDataNotice,
  SafeText,
  Stat,
  StatusBadge,
} from "@/components/ui";
import { ApiError } from "@/lib/api";

describe("StatusBadge", () => {
  it("always carries text, never colour alone", () => {
    render(
      <>
        <StatusBadge status="ok" />
        <StatusBadge status="error" />
        <StatusBadge status="incomplete" />
      </>,
    );
    expect(screen.getByText(/ok/i)).toBeInTheDocument();
    expect(screen.getByText(/error/i)).toBeInTheDocument();
    expect(screen.getByText(/incomplete/i)).toBeInTheDocument();
  });

  it("renders an unrecognised status verbatim rather than swallowing it", () => {
    render(<StatusBadge status="quarantined" />);
    expect(screen.getByText(/quarantined/i)).toBeInTheDocument();
  });
});

describe("SafeText", () => {
  it("renders attacker-influenced text as text, never as markup", () => {
    const { container } = render(
      <SafeText>{'<img src=x onerror="alert(1)">'}</SafeText>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
  });

  it("shows an em dash for absent content instead of an empty box", () => {
    render(<SafeText>{null}</SafeText>);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("ErrorState", () => {
  it("surfaces the API error code so a failure is actionable", () => {
    const error = new ApiError(429, {
      code: "rate_limited",
      message: "too many requests",
      request_id: "req-1",
    } as never);
    render(<ErrorState error={error} />);
    expect(screen.getByText(/too many requests/)).toBeInTheDocument();
    expect(screen.getByText(/rate_limited/)).toBeInTheDocument();
  });

  it("offers a retry when one is supplied", () => {
    const onRetry = vi.fn();
    render(<ErrorState error={new Error("nope")} onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalled();
  });
});

describe("state components", () => {
  it("announces loading politely", () => {
    render(<Loading label="Querying traces" />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Querying traces");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("distinguishes an empty result from a failure", () => {
    render(<EmptyState title="No traces" description="Widen the range." />);
    expect(screen.getByText("No traces")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("states why data is partial rather than silently truncating", () => {
    render(<PartialDataNotice reason="Some spans have not arrived yet." />);
    expect(
      screen.getByText(/Some spans have not arrived yet/),
    ).toBeInTheDocument();
  });
});

describe("Stat", () => {
  it("describes the direction of change in words", () => {
    render(
      <Stat
        label="Requests"
        value="1,200"
        delta={{ absolute: 200, relative: 0.2 }}
      />,
    );
    expect(
      screen.getByText(/up \(20\.0%\) vs previous period/),
    ).toBeInTheDocument();
  });

  it("inverts the judgement for metrics where up is bad", () => {
    const { container } = render(
      <Stat
        label="Error rate"
        value="2%"
        delta={{ absolute: 0.01, relative: 1 }}
        invertDelta
      />,
    );
    // The label still says "up"; only the sentiment colour differs.
    expect(container.textContent).toContain("up");
    expect(container.innerHTML).toContain("var(--warn)");
  });

  it("shows the previous value when there is no computable delta", () => {
    render(<Stat label="Cost" value="$1.00" previous="$0.90" />);
    expect(screen.getByText(/previous period: \$0\.90/)).toBeInTheDocument();
  });
});

describe("DataTable", () => {
  interface Row {
    id: string;
    name: string;
  }
  const columns = [
    { key: "name", header: "Name", render: (row: Row) => row.name },
  ];

  it("renders rows with an accessible caption", () => {
    render(
      <DataTable
        columns={columns}
        rows={[{ id: "1", name: "alpha" }]}
        rowKey={(row) => row.id}
        caption="Test rows"
      />,
    );
    expect(
      screen.getByRole("table", { name: "Test rows" }),
    ).toBeInTheDocument();
    expect(screen.getByText("alpha")).toBeInTheDocument();
  });

  it("shows the supplied empty message rather than an empty grid", () => {
    render(
      <DataTable
        columns={columns}
        rows={[]}
        rowKey={(row) => row.id}
        caption="Test rows"
        emptyMessage="Nothing here"
      />,
    );
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
  });
});
