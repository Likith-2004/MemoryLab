import { bff, expect, seed, signIn, test, XSS } from "./fixtures";

/** The corpus is written where attacker-influenced text lives: memory files and model summaries (seeded into every played run), run titles,
 *  experiment titles and report reasons. None may run (window.__xss stays undefined), and the CSP must report nothing: the `guard` fixture
 *  fails the test on either. The text must also still be there, as text. */

test("memory files, model summaries and the title render as text and never execute", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  const title = XSS.slice(0, 3).join(" ").slice(0, 120);
  const patched = await bff<{ title: string }>(page, `/runs/${s.run}`, "PATCH", { title });
  expect(patched.status).toBe(200);
  await page.goto(`/runs/${s.run}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(title);                    // the literal characters, as text
  const log = page.getByLabel("Run events");
  for (const needle of ["<img src=x onerror=", "<svg onload=", "javascript:window.__xss='js-url'", "<iframe srcdoc="]) {
    await expect(log).toContainText(needle, { timeout: 15_000 });
  }
  // every memory version of the file, through the drawer
  await page.getByRole("button", { name: /notes\/a\.md/ }).click();
  const drawer = page.getByRole("dialog", { name: "notes/a.md" });
  await expect(drawer.locator("pre")).toContainText("notes v");
  expect(await drawer.locator("pre script, pre img, pre svg, pre iframe, pre a, pre style").count()).toBe(0);
  expect(await page.locator("main script, #main script, #main iframe, #main style, #main meta, #main a[href^='javascript:']").count()).toBe(0);
  expect(await page.evaluate(() => (window as unknown as { __xss?: unknown }).__xss)).toBeUndefined();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);                                                              // give any event handler a chance to fire
});

test("run titles, experiment titles and report reasons in the lists and the admin screen are text", async ({ page, browser }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  const hostile = XSS.join(" ").slice(0, 120);
  expect((await bff(page, `/runs/${s.run}`, "PATCH", { title: hostile })).status).toBe(200);
  const exp = await bff<{ id: string }>(page, "/experiments", "POST", { title: XSS[1]!, variable: "preset_ref", run_ids: [s.run], visibility: "public" });
  expect(exp.status).toBe(201);
  await page.goto("/runs");
  await expect(page.getByRole("link", { name: hostile })).toBeVisible();
  await page.goto("/experiments");
  await expect(page.getByRole("link", { name: XSS[1]! })).toBeVisible();
  await page.goto(`/experiments/${exp.json.id}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(XSS[1]!);

  // another user reports the run with each corpus string as the reason; the admin sees them as text
  const ctx = await browser.newContext();
  const other = await ctx.newPage();
  await other.addInitScript(() => { (window as unknown as { __csp: string[] }).__csp = []; });
  await signIn(other, s.other, s.password);
  await other.goto(`/runs/${s.run}`);
  for (const reason of XSS.slice(0, 4)) expect((await bff(other, `/runs/${s.run}/report`, "POST", { reason })).status).toBe(202);
  expect(await other.evaluate(() => (window as unknown as { __xss?: unknown }).__xss)).toBeUndefined();
  await ctx.close();

  const adminCtx = await browser.newContext();
  const admin = await adminCtx.newPage();
  await admin.addInitScript(() => { document.addEventListener("securitypolicyviolation", () => { (window as unknown as { __x: number }).__x = 1; }); });
  await signIn(admin, s.admin, s.password);
  await admin.goto("/admin");
  await expect(admin.getByText("<script>window.__xss = 'script'</script>").first()).toBeVisible();
  expect(await admin.evaluate(() => (window as unknown as { __xss?: unknown; __x?: number }).__xss ?? (window as unknown as { __x?: number }).__x)).toBeUndefined();
  await adminCtx.close();
});

test("a hostile next= parameter never becomes a redirect to another site", async ({ page }) => {
  const s = seed();
  for (const next of ["https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:window.__xss=1", "/%0d%0aSet-Cookie:x=1"]) {
    await signIn(page, s.owner, s.password, next);
    expect(new URL(page.url()).origin).toBe("http://localhost:3010");
    await page.context().clearCookies();
  }
});
