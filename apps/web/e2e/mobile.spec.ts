import { expect, seed, signIn, test, waitIdle } from "./fixtures";

test.use({ viewport: { width: 360, height: 740 }, isMobile: true, hasTouch: true });

async function noHorizontalScroll(page: import("@playwright/test").Page, label: string): Promise<void> {
  const [scroll, client] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(scroll, `${label} scrolls sideways (${scroll} > ${client})`).toBeLessThanOrEqual(client);
}

test("at 360 px no screen scrolls sideways", async ({ page }) => {
  const s = seed();
  for (const path of ["/", "/auth/signin", "/auth/signup", "/leaderboard", "/privacy", "/terms", "/licences", "/nope"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await noHorizontalScroll(page, path);
  }
  await signIn(page, s.admin, s.password);
  for (const path of ["/runs", "/runs/new", "/experiments", "/settings", "/admin", `/runs/${s.run}`]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await noHorizontalScroll(page, path);
  }
});

test("on a phone the run page is the game on top, then tabs; Stop stays in the top bar", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password, `/runs/${s.run}`);
  const frame = page.getByRole("figure");
  const tabs = page.getByRole("tablist", { name: "Run panels" });
  await expect(tabs).toBeVisible();
  for (const name of ["Log", "Memory", "Budget", "Checkpoints"]) await expect(tabs.getByRole("tab", { name })).toBeVisible();
  const fb = (await frame.boundingBox())!;
  const tb = (await tabs.boundingBox())!;
  expect(fb.y).toBeLessThan(tb.y);                                               // the game is above the tabs
  await tabs.getByRole("tab", { name: "Memory" }).click();
  await expect(page.getByRole("heading", { name: /Memory at step/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Log" })).toBeHidden();
  await tabs.getByRole("tab", { name: "Budget" }).click();
  await expect(page.getByRole("heading", { name: "Budget" })).toBeVisible();
  await noHorizontalScroll(page, "run page with the budget tab");
});

test("a run in progress shows Stop in the sticky status strip while the page is scrolled", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password, "/runs/new");
  await waitIdle(page);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Game").selectOption(s.game);
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Review" }).click();
  await page.getByLabel("Your provider API key").fill("sk-ant-CANARY-0123456789abcdef");
  await page.getByRole("button", { name: "Start run" }).click();
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/);
  const stop = page.getByRole("button", { name: "Stop run" });
  await expect(stop).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const b = (await stop.boundingBox())!;
  expect(b.y).toBeGreaterThanOrEqual(0);
  expect(b.y + b.height).toBeLessThanOrEqual(740);                               // still on screen after scrolling
  await stop.click();
  await expect(page.getByRole("status").filter({ hasText: /Stop requested|Run completed/ }).first()).toBeVisible({ timeout: 20_000 });
});
