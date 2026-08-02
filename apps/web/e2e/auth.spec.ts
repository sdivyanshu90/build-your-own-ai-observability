import { expect, test } from "@playwright/test";

import { E2E_EMAIL, E2E_PASSWORD, signIn } from "./helpers";

test.describe("authentication", () => {
  test("an unauthenticated deep link returns you to where you were going", async ({
    page,
  }) => {
    await page.goto("/traces?quick=status%3Aeq%3Aerror");
    await expect(page).toHaveURL(/\/login\?next=/);

    await page.getByLabel("Email").fill(E2E_EMAIL);
    await page.getByLabel("Password").fill(E2E_PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(/\/traces/);
  });

  test("a wrong password is rejected without revealing whether the account exists", async ({
    page,
  }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill("definitely-not-a-user@example.test");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    // Scoped to the form: Next.js renders its own role="alert" route
    // announcer, so an unscoped query is ambiguous.
    const alert = page.locator("form").getByRole("alert");
    await expect(alert).toBeVisible();
    const unknownAccountMessage = await alert.textContent();

    await page.getByLabel("Email").fill(E2E_EMAIL);
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    // Identical wording: the form must not be an account-enumeration oracle.
    await expect(alert).toHaveText(unknownAccountMessage ?? "");
  });

  test("the token is held in sessionStorage, never in a cookie", async ({
    page,
  }) => {
    await signIn(page);
    const token = await page.evaluate(() =>
      window.sessionStorage.getItem("aiobs.access_token"),
    );
    expect(token).toBeTruthy();
    const cookies = await page.context().cookies();
    expect(cookies.some((cookie) => cookie.value === token)).toBe(false);
  });

  test("signing out clears the session and returns to the login page", async ({
    page,
  }) => {
    await signIn(page);
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login/);
    const token = await page.evaluate(() =>
      window.sessionStorage.getItem("aiobs.access_token"),
    );
    expect(token).toBeNull();
  });
});
