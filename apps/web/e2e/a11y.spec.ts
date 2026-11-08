import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { expect, seed, signIn, test } from "./fixtures";

async function clean(page: Page, label: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).analyze();
  const bad = results.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
  expect(bad, `${label}\n${bad.join("\n")}`).toEqual([]);
}

test("public screens have no axe violations", async ({ page }) => {
  for (const path of ["/", "/auth/signin", "/auth/signup", "/auth/reset", "/auth/magic", "/leaderboard", "/privacy", "/terms", "/licences", "/nope-404"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await clean(page, path);
  }
});

test("signed-in screens, every New run step, the run page (live, replay, drawer) and admin have no axe violations", async ({ page }) => {
  const s = seed();
  await signIn(page, s.admin, s.password, "/runs/new");
  await clean(page, "/runs/new step 1");
  await page.getByRole("button", { name: "Next" }).click();
  await clean(page, "/runs/new step 2");
  await page.getByLabel("Game").selectOption(s.game);
  await page.getByRole("button", { name: "Next" }).click();
  await clean(page, "/runs/new step 3");
  await page.getByRole("button", { name: "Next" }).click();
  await clean(page, "/runs/new step 4");
  await page.getByRole("button", { name: "Review" }).click();
  await expect(page.getByText("Config hash")).toBeVisible();
  await clean(page, "/runs/new review");

  for (const path of ["/runs", "/experiments", "/settings", "/admin"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await clean(page, path);
  }
  await page.goto(`/runs/${s.run}`);
  await expect(page.getByLabel("Run events")).toContainText("model call");
  await clean(page, "run page, replay");
  await page.getByRole("slider", { name: "Step" }).fill("2");
  await clean(page, "run page, scrubbed");
  await page.getByRole("button", { name: /notes\/a\.md/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await clean(page, "memory drawer");
});

test("the dark colour scheme has no axe violations either", async ({ page }) => {
  const s = seed();
  await page.emulateMedia({ colorScheme: "dark" });
  for (const path of ["/", "/auth/signin", "/leaderboard", "/privacy"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await clean(page, `dark ${path}`);
  }
  await signIn(page, s.owner, s.password, `/runs/${s.run}`);
  await expect(page.getByLabel("Run events")).toContainText("model call");
  await clean(page, "dark run page");
  await page.getByRole("button", { name: /notes\/a\.md/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await clean(page, "dark memory drawer");
});

test("with reduced motion no animation runs on the log", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/runs/${seed().run}`);
  await expect(page.getByLabel("Run events")).toContainText("model call");
  const running = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === "running").length);
  expect(running).toBe(0);
});
