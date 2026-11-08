import { CANARY, bff, control, expect, seed, signIn, test, waitIdle } from "./fixtures";
import type { Page } from "@playwright/test";

async function launch(page: Page, key = CANARY): Promise<void> {
  const s = seed();
  await page.goto("/runs/new");
  await waitIdle(page);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Game").selectOption(s.game);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Review" }).click();
  await page.getByLabel("Your provider API key").fill(key);
  await page.getByRole("button", { name: "Start run" }).click();
}

test.afterEach(async () => { await control("mode/ok"); await control("delay/350"); });

test("capacity_full: the lab is full, with a retry hint, and the form survives", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  await control("mode/capacity_full");
  await launch(page);
  await expect(page.getByRole("alert").filter({ hasText: "The lab is full right now" })).toContainText(/Retry in about \d+ seconds/);
  await expect(page.getByLabel("Your provider API key")).toHaveValue("");                    // cleared after the attempt
  await control("mode/ok");
  await page.getByLabel("Your provider API key").fill(CANARY);
  await page.getByRole("button", { name: "Start run" }).click();                              // the same screen works once there is room
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/);
});

test("key_rejected: one generic message on the key field, the key is cleared", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  await launch(page, "sk-ant-wrong-key-123456");
  await expect(page.getByText("That provider key was not accepted. Check it and try again.")).toBeVisible();
  await expect(page.getByLabel("Your provider API key")).toHaveValue("");
  expect(await page.content()).not.toContain("sk-ant-wrong-key-123456");
});

test("invalid caps stop the form before anything is sent", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password, "/runs/new");
  for (let i = 0; i < 3; i++) await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Max steps").fill("0");
  await expect(page.getByRole("button", { name: "Review" })).toBeDisabled();
  await page.getByLabel("Max steps").fill("100001");
  await expect(page.getByRole("button", { name: "Review" })).toBeDisabled();
  await page.getByLabel("Max steps").fill("50");
  await expect(page.getByRole("button", { name: "Review" })).toBeEnabled();
});

test("every lifecycle state is shown as it happens: queued, starting, running, session boundary, dreaming, ended", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  await control("delay/1500");
  await launch(page);
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/);
  const strip = page.getByRole("region", { name: "Run status" });
  const notes = page.getByRole("status");
  await expect(notes.filter({ hasText: "Queued: waiting for a free slot." })).toBeVisible({ timeout: 10_000 });
  await expect(notes.filter({ hasText: "Starting: preparing the sandbox." })).toBeVisible({ timeout: 10_000 });
  await expect(strip).toContainText("running", { timeout: 10_000 });
  await expect(notes.filter({ hasText: "Session boundary" })).toBeVisible({ timeout: 40_000 });
  await expect(notes.filter({ hasText: "Dreaming" })).toBeVisible({ timeout: 10_000 });
  await expect(notes.filter({ hasText: /^Run completed/ })).toBeVisible({ timeout: 40_000 });
});

test("disconnected: the live view says it is resuming, then falls back to polling, and the run still arrives", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  await control("delay/600");
  await page.routeWebSocket(/\/ws\/runs\//, (ws) => { void ws.close({ code: 1006, reason: "blocked" }); });
  await launch(page);
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/);
  await expect(page.getByText("Disconnected. Resuming the live view…")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("The live connection is unavailable; updating every few seconds.")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("status").filter({ hasText: /^Run completed/ })).toBeVisible({ timeout: 40_000 });
  await expect(page.getByLabel("Run events")).toContainText("completed");
});

test("a dropped socket reconnects with a new ticket and resumes without repeating events", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  await control("delay/700");
  let first = true;
  await page.routeWebSocket(/\/ws\/runs\//, (ws) => {
    const server = ws.connectToServer();
    if (first) { first = false; setTimeout(() => void ws.close({ code: 1006 }), 2500); }
    void server;
  });
  await launch(page);
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("status").filter({ hasText: /^Run completed/ })).toBeVisible({ timeout: 40_000 });
  const seqs = await page.getByLabel("Run events").locator("li").count();
  const expected = await page.evaluate(async () => {
    const id = location.pathname.split("/").pop();
    const r = await fetch(`/bff/api/runs/${id}/events?limit=1000`, { headers: { "x-memorylab-csrf": "1" } });
    return ((await r.json()) as { events: unknown[] }).events.length;
  });
  expect(seqs).toBe(expected);                                                                // nothing missing, nothing twice
});

test("experiments warn below five runs and name the single changed field", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  await control("delay/60");
  await waitIdle(page);
  const ids: string[] = [];
  for (const preset of ["general@1", "none@1"]) {
    const r = await bff<{ run_id: string }>(page, "/runs", "POST", { game: s.game, visibility: "public", api_key: CANARY,
      config: { provider: "anthropic", model_id: "claude-haiku-5-5", preset_ref: preset, mode: "demo" }, caps: { max_steps: 50, max_tokens: 100000, max_wall_s: 600 } });
    expect(r.status).toBe(202);
    ids.push(r.json.run_id);
    await expect.poll(async () => (await bff<{ status: string }>(page, `/runs/${r.json.run_id}`)).json.status, { timeout: 30_000 }).toBe("completed");
  }
  const e = await bff<{ id: string }>(page, "/experiments", "POST", { title: "Preset arms", variable: "preset_ref", run_ids: ids, visibility: "public" });
  expect(e.status).toBe(201);
  await page.goto(`/experiments/${e.json.id}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Preset arms");
  await expect(page.getByText("Fewer than 5 scored runs: this is not a finding yet.")).toBeVisible();
  await expect(page.getByText(/Runs differ only in\s+preset_ref/)).toBeVisible();
  await expect(page.getByRole("img", { name: /score by step for 2 runs/ })).toBeVisible();
  await expect(page.getByRole("table", { name: "Runs in this experiment" }).getByRole("row")).toHaveCount(3);
});

test("the breaker banner: an admin opens it, sees it, and closes it; every action needs a reason", async ({ page }) => {
  const s = seed();
  await signIn(page, s.admin, s.password, "/admin");
  await expect(page.getByRole("button", { name: "Open breaker" })).toBeDisabled();            // no reason yet
  await page.getByLabel(/^Reason/).fill("provider outage");
  await page.getByRole("button", { name: "Open breaker" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "The breaker is OPEN" })).toContainText("provider outage");
  await page.getByRole("button", { name: "Close breaker" }).click();
  await expect(page.getByText("The breaker is OPEN")).toBeHidden();
});

test("the admin screen does not exist for a normal user, and the API agrees", async ({ page }) => {
  const s = seed();
  await signIn(page, s.other, s.password);
  const r = await page.goto("/admin");
  expect(r?.status()).toBe(404);
  expect((await bff(page, "/admin/runs")).status).toBe(404);
  await expect(page.getByRole("link", { name: "Admin" })).toHaveCount(0);
});

test("the account screen: sign out everywhere, and the delete flow needs the exact word", async ({ page, browser }) => {
  const s = seed();
  const ctx = await browser.newContext();
  const p = await ctx.newPage();
  await signIn(p, s.other, s.password, "/settings");
  await expect(p.getByRole("button", { name: "Delete my account" })).toBeDisabled();
  await p.getByLabel("Type DELETE to confirm").fill("delete");
  await expect(p.getByRole("button", { name: "Delete my account" })).toBeDisabled();
  await p.getByRole("button", { name: "Sign out everywhere" }).click();
  await p.waitForURL((u) => u.pathname === "/");
  await p.goto("/settings");
  await p.waitForURL(/\/auth\/signin/);                                                      // the session is gone
  await ctx.close();
  void page;
});
