import { expect, seed, test } from "./fixtures";

/** An attacker must not learn whether an address has an account from anything the pages show. */
test("sign-up, magic link and reset say the same thing for a known and an unknown address", async ({ page }) => {
  const s = seed();
  const unknown = `nobody-${Date.now()}@e2e.test`;
  const flows: [string, (email: string) => Promise<void>][] = [
    ["/auth/signup", async (email) => { await page.getByLabel("Email").fill(email); await page.getByLabel("Password").fill("a-long-password-1"); await page.getByRole("button", { name: "Create account" }).click(); }],
    ["/auth/magic", async (email) => { await page.getByLabel("Email").fill(email); await page.getByRole("button", { name: "Send link" }).click(); }],
    ["/auth/reset", async (email) => { await page.getByLabel("Email").fill(email); await page.getByRole("button", { name: "Send reset email" }).click(); }],
  ];
  for (const [path, submit] of flows) {
    const seen: string[] = [];
    for (const email of [s.other, unknown]) {
      await page.goto(path);
      await submit(email);
      await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();
      seen.push((await page.getByRole("status").innerText()).trim());
    }
    expect(seen[0], path).toBe(seen[1]);
    expect(seen[0]).toBe("If an account exists, we've sent an email.");
  }
});

test("sign-in says one thing for a wrong password and an unknown address", async ({ page }) => {
  const s = seed();
  const seen: string[] = [];
  for (const email of [s.owner, `nobody-${Date.now()}@e2e.test`]) {
    await page.goto("/auth/signin");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("definitely-not-the-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toBeVisible();
    seen.push((await page.getByRole("alert").filter({ hasText: /\S/ }).innerText()).trim());
    await expect(page.getByLabel("Password")).toHaveValue("");                                   // cleared after every attempt
  }
  expect(seen[0]).toBe(seen[1]);
});

test("a short password is refused for everyone alike, before any account is touched", async ({ page }) => {
  const s = seed();
  const messages: string[] = [];
  for (const email of [s.other, `nobody-${Date.now()}@e2e.test`]) {
    await page.goto("/auth/signup");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("short");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "at least 12 characters" })).toBeVisible();
    messages.push(await page.getByRole("alert").filter({ hasText: "at least 12 characters" }).innerText());
  }
  expect(messages[0]).toBe(messages[1]);
});
