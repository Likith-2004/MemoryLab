import { chromium } from "@playwright/test";
import { expect, test } from "./fixtures";

/** Lighthouse (performance, accessibility, best practices, SEO) on the two public pages the spec names. Run against the production build. */
test("Lighthouse scores at least 90 on Landing and Leaderboard", async () => {
  test.setTimeout(180_000);
  const { default: lighthouse } = await import("lighthouse");
  const browser = await chromium.launch({ args: ["--remote-debugging-port=9333"] });
  try {
    for (const path of ["/", "/leaderboard"]) {
      const result = await lighthouse(`http://localhost:3010${path}`, { port: 9333, output: "json", logLevel: "error", onlyCategories: ["performance", "accessibility", "best-practices", "seo"] });
      const cats = result!.lhr.categories;
      const scores = Object.fromEntries(Object.entries(cats).map(([k, v]) => [k, Math.round((v.score ?? 0) * 100)]));
      console.log(`lighthouse ${path}`, JSON.stringify(scores));
      for (const [name, score] of Object.entries(scores)) expect(score, `${path} ${name}`).toBeGreaterThanOrEqual(90);
    }
  } finally {
    await browser.close();
  }
});
