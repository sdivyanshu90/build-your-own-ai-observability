import { expect, type Page } from "@playwright/test";

/**
 * Credentials come from the environment so the suite can run against a stack
 * seeded by `make bootstrap`. The defaults match the documented development
 * bootstrap; they are not valid anywhere a real password was set.
 */
export const E2E_EMAIL = process.env.AIOBS_E2E_EMAIL ?? "admin@example.com";
export const E2E_PASSWORD =
  process.env.AIOBS_E2E_PASSWORD ?? "change-me-immediately-please";

/**
 * Which environment the demo data was seeded into. `aiobs-admin seed-demo`
 * writes to `development` by default, while the application quite correctly
 * defaults a human to production — so the suite selects it explicitly rather
 * than depending on which one happens to be first.
 */
export const E2E_ENVIRONMENT =
  process.env.AIOBS_E2E_ENVIRONMENT ?? "development";

/** Longest a query-driven region is allowed to stay in its loading state. */
export const SETTLE_TIMEOUT = 20_000;

export async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(E2E_EMAIL);
  await page.getByLabel("Password").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/(?!login)/);
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
  await selectEnvironment(page);
}

/** Point the workspace at the environment holding the demo data. */
export async function selectEnvironment(page: Page): Promise<void> {
  // Scoped to the header: some pages have their own Environment control.
  const picker = page.getByRole("banner").getByLabel("Environment");
  await expect(picker).toBeEnabled({ timeout: SETTLE_TIMEOUT });
  const options = await picker.locator("option").allTextContents();
  const match = options.find((option) => option.startsWith(E2E_ENVIRONMENT));
  if (match) await picker.selectOption({ label: match });
}

/** Wait for a query-driven region to settle: no "Loading…" left on the page. */
export async function settled(page: Page): Promise<void> {
  await expect(page.getByRole("status").filter({ hasText: /…$/ })).toHaveCount(
    0,
    {
      timeout: SETTLE_TIMEOUT,
    },
  );
}

/** Navigate within the app, keeping the seeded environment selected. */
export async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await selectEnvironment(page);
  await settled(page);
}

/** Open the first trace in the explorer, returning its id. */
export async function openFirstTrace(page: Page): Promise<string> {
  await visit(page, "/traces");
  // Scoped to the results table: the page header also links to
  // /traces/compare, which is not a trace.
  const link = tracesLinks(page).first();
  await expect(link).toBeVisible();
  const href = await link.getAttribute("href");
  await link.click();
  await expect(
    page.getByRole("tablist", { name: "Trace views" }),
  ).toBeVisible();
  return (href ?? "").replace("/traces/", "").split("?")[0] ?? "";
}

/** Links to individual traces in the results table. */
export function tracesLinks(page: Page) {
  return page.getByRole("table").locator('a[href^="/traces/"]');
}
