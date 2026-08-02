import { expect, test } from "@playwright/test";

import { openFirstTrace, settled, signIn, tracesLinks, visit } from "./helpers";

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test.describe("overview", () => {
  test("shows the headline metrics for the selected window", async ({
    page,
  }) => {
    await visit(page, "/");
    for (const label of [
      "Requests",
      "Error rate",
      "p95 latency",
      "Tokens",
      "Cost",
    ]) {
      await expect(
        page.getByText(label, { exact: true }).first(),
      ).toBeVisible();
    }
  });

  test("charts expose their data as a table for assistive technology", async ({
    page,
  }) => {
    await visit(page, "/");
    await expect(
      page.locator("table caption", { hasText: /Chart data/ }).first(),
    ).toBeAttached();
  });
});

test.describe("trace explorer", () => {
  test("filters are addressable: the URL alone reproduces the view", async ({
    page,
  }) => {
    await visit(page, "/traces");

    await page.getByLabel("Quick filter").selectOption("status:eq:error");
    await expect(page).toHaveURL(/quick=status%3Aeq%3Aerror/);

    await page.reload();
    await settled(page);
    await expect(page.getByLabel("Quick filter")).toHaveValue(
      "status:eq:error",
    );
  });

  test("sorting by cost re-queries the server rather than sorting the page", async ({
    page,
  }) => {
    await visit(page, "/traces");
    await page.getByLabel("Sort").selectOption("-cost");
    await expect(page).toHaveURL(/sort=-cost/);
    await settled(page);
    await expect(page.getByRole("table")).toBeVisible();
  });

  test("an invalid expression filter is reported, not silently ignored", async ({
    page,
  }) => {
    await visit(page, "/traces");
    await page
      .getByLabel("Expression filter (field:op:value)")
      .fill("nonexistent_field:eq:1");
    await page.getByRole("button", { name: "Apply" }).click();
    // Scoped to the results card: Next.js also renders a role="alert" announcer.
    await expect(
      page.getByRole("alert").filter({ hasText: /Could not load/ }),
    ).toBeVisible();
  });
});

test.describe("trace detail", () => {
  test("renders the waterfall and lets a span be selected by URL", async ({
    page,
  }) => {
    await openFirstTrace(page);

    const tree = page.getByRole("tree", { name: "Span waterfall" });
    await expect(tree).toBeVisible();

    const rows = tree.getByRole("treeitem");
    await expect(rows.first()).toBeVisible();
    await rows.first().click();

    await expect(page).toHaveURL(/span=/);
  });

  test("the waterfall is navigable by keyboard", async ({ page }) => {
    await openFirstTrace(page);
    const rows = page
      .getByRole("tree", { name: "Span waterfall" })
      .getByRole("treeitem");
    await rows.first().focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/span=/);
  });

  test("switches to the retrieval and trajectory views", async ({ page }) => {
    await openFirstTrace(page);

    await page.getByRole("tab", { name: /Retrieval/ }).click();
    await expect(page).toHaveURL(/tab=retrieval/);
    await settled(page);

    await page.getByRole("tab", { name: /Agent trajectory/ }).click();
    await expect(page).toHaveURL(/tab=trajectory/);
    await settled(page);
  });

  test("shows lineage so a response can be attributed to a version", async ({
    page,
  }) => {
    await openFirstTrace(page);
    await page.getByRole("tab", { name: "Metadata & lineage" }).click();
    await expect(page.getByRole("heading", { name: "Lineage" })).toBeVisible();
    await expect(page.getByText("Prompt versions")).toBeVisible();
  });

  test("two traces can be compared", async ({ page }) => {
    await visit(page, "/traces");
    const links = tracesLinks(page);
    const first = (await links.nth(0).getAttribute("href"))!.replace(
      "/traces/",
      "",
    );
    const second = (await links.nth(1).getAttribute("href"))!.replace(
      "/traces/",
      "",
    );

    await page.goto(`/traces/compare?left=${first}&right=${second}`);
    await settled(page);

    await expect(page.getByRole("heading", { name: "Deltas" })).toBeVisible();
    await expect(page.getByText("Lineage differences")).toBeVisible();
  });
});
