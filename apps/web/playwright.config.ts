import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end configuration.
 *
 * These tests drive the real browser against a real API. They are deliberately
 * *not* run against mocks: the failures worth catching here — a filter that the
 * server rejects, a cursor that does not round-trip, a 401 loop — only happen
 * when both halves are present.
 *
 * `AIOBS_E2E_BASE_URL` and `AIOBS_E2E_API_URL` let CI point at an already
 * running stack. Locally, the web server is started for you.
 */

const baseURL = process.env.AIOBS_E2E_BASE_URL ?? "http://127.0.0.1:53000";
const apiURL = process.env.AIOBS_E2E_API_URL ?? "http://127.0.0.1:58000";

export default defineConfig({
  testDir: "./e2e",
  // A trace that renders slowly is a bug, but CI machines are not fast; this is
  // generous enough not to flake and short enough to notice a hang.
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    extraHTTPHeaders: { "accept-language": "en-GB" },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: process.env.AIOBS_E2E_BASE_URL
    ? undefined
    : {
        command: "npm run start",
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        env: { NEXT_PUBLIC_API_URL: apiURL, PORT: "53000" },
      },
});
