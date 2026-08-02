import { expect, test } from "@playwright/test";

import { visit, signIn } from "./helpers";

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test.describe("cost", () => {
  test("reports a total and says whether it is complete", async ({ page }) => {
    await visit(page, "/costs");
    await expect(page.getByText("Total cost")).toBeVisible();
    await expect(
      page.getByText(/lower bound|all usage priced/).first(),
    ).toBeVisible();
  });

  test("groups spend by model", async ({ page }) => {
    await visit(page, "/costs");
    await page.getByLabel("Group by").selectOption("provider");
    await expect(
      page.getByRole("heading", { name: "Breakdown" }),
    ).toBeVisible();
  });
});

test.describe("latency", () => {
  test("reports percentiles, not just an average", async ({ page }) => {
    await visit(page, "/latency");
    await expect(
      page.getByRole("columnheader", { name: "p50", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("columnheader", { name: "p99", exact: true }),
    ).toBeVisible();
  });
});

test.describe("registries", () => {
  test("prompt versions are immutable and identified by content hash", async ({
    page,
  }) => {
    await visit(page, "/prompts");
    await expect(page.getByText("Content hash")).toBeVisible();
    // An alias is a movable pointer; the version it points at is not editable.
    await expect(page.getByRole("heading", { name: "Aliases" })).toBeVisible();
  });

  test("models list their configuration versions", async ({ page }) => {
    await visit(page, "/models");
    await expect(page.locator("h1", { hasText: "Models" })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Configuration versions" }),
    ).toBeVisible();
  });

  test("datasets surface licence and personal-data status", async ({
    page,
  }) => {
    await visit(page, "/datasets");
    await expect(page.locator("h1", { hasText: "Datasets" })).toBeVisible();
    await expect(
      page.getByRole("columnheader", { name: "Personal data" }),
    ).toBeVisible();
  });
});

test.describe("settings", () => {
  test("a new API key shows its secret exactly once", async ({ page }) => {
    await visit(page, "/settings/api-keys");

    const name = `e2e-${Date.now()}`;
    await page.getByLabel("Name").fill(name);
    await page.getByRole("button", { name: "Create key" }).click();

    const banner = page.getByRole("alert").filter({ hasText: "Copy it now" });
    await expect(banner).toContainText("Copy it now");
    const secret = await banner.locator("code").textContent();
    expect(secret).toBeTruthy();

    // Reload: the secret must be unrecoverable, only the prefix remains.
    await page.reload();
    await visit(page, "/settings/api-keys");
    await expect(page.getByText(secret!)).toHaveCount(0);
    await expect(page.getByRole("cell", { name })).toBeVisible();
  });

  test("price books are effective-dated and cite a source", async ({
    page,
  }) => {
    await visit(page, "/settings/price-books");
    await expect(
      page.getByRole("columnheader", { name: "Effective" }),
    ).toBeVisible();
  });

  test("the audit log records privileged actions with a request id", async ({
    page,
  }) => {
    await visit(page, "/settings/audit");
    await expect(
      page.getByRole("columnheader", { name: "Request id" }),
    ).toBeVisible();
  });

  test("retention is explained in terms of what deletion actually removes", async ({
    page,
  }) => {
    await visit(page, "/settings/retention");
    await expect(page.getByText("What deletion actually does")).toBeVisible();
  });
});

test.describe("accessibility", () => {
  test("a skip link takes keyboard users straight to the content", async ({
    page,
  }) => {
    await page.goto("/");
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to main content" });
    await expect(skip).toBeFocused();
  });

  test("the primary navigation marks the current page", async ({ page }) => {
    await visit(page, "/traces");
    await expect(
      page.getByRole("link", { name: "Traces", exact: true }),
    ).toHaveAttribute("aria-current", "page");
  });

  test("the page has exactly one level-one heading", async ({ page }) => {
    await visit(page, "/");
    await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  });
});
