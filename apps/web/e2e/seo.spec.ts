import { bff, CANARY, control, expect, seed, signIn, test, waitIdle } from "./fixtures";

/** Launch checklist: titles/OG/robots/sitemap; private and unlisted runs are noindex; a 404 page; a favicon. Against the production build. */

const robotsMeta = (html: string) => /<meta name="robots" content="([^"]*)"/.exec(html)?.[1] ?? null;

test("robots.txt and sitemap.xml list the public pages and the verified run, and keep the private surfaces out", async ({ request }) => {
  const s = seed();
  const robots = await (await request.get("/robots.txt")).text();
  expect(robots).toMatch(/Sitemap: http:\/\/localhost:3010\/sitemap\.xml|Sitemap: .*\/sitemap\.xml/);
  for (const p of ["/admin", "/settings", "/bff/", "/auth/", "/runs/new"]) expect(robots).toContain(`Disallow: ${p}`);
  expect(robots).not.toMatch(/Disallow: \/runs\/?\s/);                    // run pages stay crawlable so their own noindex is seen
  const sitemap = await (await request.get("/sitemap.xml")).text();
  for (const p of ["/leaderboard", "/privacy", "/terms", "/licences"]) expect(sitemap).toContain(`${p}</loc>`);
  expect(sitemap).toContain(`/runs/${s.run}</loc>`);                      // the seeded run is public and verified
  expect(sitemap).not.toMatch(/\/(admin|settings|auth|bff)\b/);
});

test("a private run and an unlisted run are noindex and out of the sitemap; a public run is indexable with its own OG tags", async ({ page, request }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  await control("delay/10");
  const ids: Record<string, string> = {};
  for (const visibility of ["private", "unlisted"]) {
    await waitIdle(page);
    const r = await bff<{ run_id: string }>(page, "/runs", "POST", { game: s.game, visibility, api_key: CANARY,
      config: { provider: "anthropic", model_id: "claude-haiku-5-5", preset_ref: "general@1", mode: "demo" }, caps: { max_steps: 50, max_tokens: 100000, max_wall_s: 600 } });
    expect(r.status).toBe(202);
    ids[visibility] = r.json.run_id;
  }
  for (const [visibility, id] of Object.entries(ids)) {
    const html = await (await page.request.get(`/runs/${id}`)).text();         // with the owner's session, so the run is visible to them
    expect(robotsMeta(html), visibility).toMatch(/noindex/);
    expect(html).not.toMatch(/property="og:title" content="[^"]*Seeded/);
  }
  const sitemap = await (await request.get("/sitemap.xml")).text();
  for (const id of Object.values(ids)) expect(sitemap).not.toContain(id);
  const pub = await (await request.get(`/runs/${s.run}`)).text();
  expect(robotsMeta(pub)).toBeNull();
  expect(pub).toMatch(/property="og:title"/);
  expect(pub).toMatch(/property="og:site_name" content="Memory Lab"|property="og:description"/);
  await waitIdle(page);
});

test("the draft legal pages are reachable, say they are drafts, and the 404 page is ours", async ({ page, request }) => {
  for (const p of ["/privacy", "/terms", "/licences"]) {
    const r = await request.get(p);
    expect(r.status(), p).toBe(200);
    expect(await r.text(), p).toMatch(/draft/i);
  }
  const missing = await page.goto("/no-such-page");
  expect(missing?.status()).toBe(404);
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
  expect(missing?.headers()["content-security-policy"]).toContain("frame-ancestors 'none'");     // the 404 carries the headers too
  expect(missing?.headers()["strict-transport-security"]).toMatch(/max-age=\d{8,}/);
});

test("there is a favicon, and the OG image is served", async ({ request, page }) => {
  await page.goto("/");
  const icon = await page.locator('link[rel="icon"]').first().getAttribute("href");
  expect(icon).toBeTruthy();
  expect((await request.get(icon!)).status()).toBe(200);
  const og = await page.locator('meta[property="og:image"]').first().getAttribute("content");
  expect(og).toBeTruthy();
  const img = await request.get(new URL(og!).pathname);
  expect(img.status()).toBe(200);
  expect(img.headers()["content-type"]).toContain("image/png");
});
