import { CANARY, bff, expect, seed, signIn, test, waitIdle } from "./fixtures";

test("the browser only ever talks to this site (and the API's signed frame URLs and socket), never to GoTrue", async ({ page }) => {
  const s = seed();
  const urls: string[] = [];
  page.on("request", (r) => urls.push(r.url()));
  await page.goto("/auth/signup");
  await page.getByLabel("Email").fill(s.other);
  await page.getByLabel("Password").fill("a-long-password-1");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();
  await page.goto("/auth/magic");
  await page.getByLabel("Email").fill(s.other);
  await page.getByRole("button", { name: "Send link" }).click();
  await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();
  await page.goto("/auth/reset");
  await page.getByLabel("Email").fill(s.other);
  await page.getByRole("button", { name: "Send reset email" }).click();
  await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();
  await signIn(page, s.owner, s.password, `/runs/${s.run}`);
  await expect(page.getByLabel("Run events")).toContainText("model call");
  const hosts = new Set(urls.map((u) => new URL(u).host));
  expect([...hosts].sort()).toEqual(expect.arrayContaining(["localhost:3010"]));
  for (const h of hosts) expect(["localhost:3010", "127.0.0.1:8010"], `unexpected host ${h}`).toContain(h);   // the API host: frames and the socket only
  expect(urls.filter((u) => /\/auth\/v1\//.test(u) || /:54321/.test(u))).toEqual([]);
  expect(urls.filter((u) => new URL(u).host === "127.0.0.1:8010").every((u) => /\/_blob\/|\/ws\/runs\//.test(u))).toBe(true);
  const sent = urls.filter((u) => new URL(u).pathname.startsWith("/bff/api/auth/"));
  expect(sent.map((u) => new URL(u).pathname).sort()).toEqual(["/bff/api/auth/magic-link", "/bff/api/auth/recover", "/bff/api/auth/signup"]);
});

test("the session is two httpOnly SameSite=Strict cookies and nothing a script can read", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password, `/runs/${s.run}`);
  await expect(page.getByLabel("Run events")).toContainText("model call");
  const cookies = await page.context().cookies();
  const mine = cookies.filter((c) => /^(__Host-)?ml_(at|rt)$/.test(c.name));
  expect(mine.map((c) => c.name).sort()).toEqual(["ml_at", "ml_rt"]);
  for (const c of mine) {
    expect(c.httpOnly).toBe(true);
    expect(c.sameSite).toBe("Strict");
    expect(c.path).toBe("/");
  }
  const visible = await page.evaluate(() => JSON.stringify({ cookie: document.cookie, ls: { ...localStorage }, ss: { ...sessionStorage } }));
  const token = mine.find((c) => c.name === "ml_at")!.value;
  expect(visible).not.toContain(token.slice(0, 30));
  expect(visible).not.toContain(mine.find((c) => c.name === "ml_rt")!.value);
  expect(visible).not.toMatch(/eyJ[A-Za-z0-9_-]{8,}\./);
});

test("the pages carry the security headers and a fresh nonce per request", async ({ request }) => {
  const a = await request.get("/leaderboard");
  const b = await request.get("/leaderboard");
  const csp = a.headers()["content-security-policy"]!;
  expect(csp).not.toContain("unsafe-inline");
  expect(csp).not.toContain("unsafe-eval");
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toContain("report-uri /csp-report");
  const nonce = (h: string) => /'nonce-([^']+)'/.exec(h)![1];
  expect(nonce(csp)).not.toBe(nonce(b.headers()["content-security-policy"]!));
  const html = await a.text();
  for (const tag of html.match(/<script\b[^>]*>/g) ?? []) expect(tag, tag).toContain(`nonce="${nonce(csp)}"`);   // every script carries it
  expect(html).not.toMatch(/\son[a-z]+=/i);                                                                      // no inline event handlers
  const h = a.headers();
  expect(h["strict-transport-security"]).toMatch(/max-age=\d{8,}/);
  expect(h["x-frame-options"]).toBe("DENY");
  expect(h["referrer-policy"]).toBe("no-referrer");
  expect(h["permissions-policy"]).toContain("camera=()");
  expect(h["x-content-type-options"]).toBe("nosniff");
  expect(h["x-powered-by"]).toBeUndefined();
});

test("state-changing requests need our own origin and header", async ({ page, request }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  const cookies = (await page.context().cookies()).map((c) => `${c.name}=${c.value}`).join("; ");
  const body = JSON.stringify({ title: "pwned" });
  const attempt = async (headers: Record<string, string>) =>
    (await request.patch(`/bff/api/runs/${s.run}`, { headers: { "content-type": "application/json", cookie: cookies, ...headers }, data: body })).status();
  expect(await attempt({ origin: "http://evil.example", "x-memorylab-csrf": "1" })).toBe(403);   // foreign Origin
  expect(await attempt({ "x-memorylab-csrf": "1" })).toBe(403);                                  // no Origin, no Sec-Fetch-Site
  expect(await attempt({ origin: "http://localhost:3010" })).toBe(403);                          // our Origin, no custom header
  expect(await attempt({ "sec-fetch-site": "cross-site", "x-memorylab-csrf": "1" })).toBe(403);
  expect(await attempt({ origin: "http://localhost:3010", "x-memorylab-csrf": "1" })).toBe(200);
  const res = await bff<{ title: string }>(page, `/runs/${s.run}`);
  expect(res.json.title).toBe("pwned");
  const logout = await request.post("/bff/auth/logout", { headers: { cookie: cookies, origin: "http://evil.example", "x-memorylab-csrf": "1", "content-type": "application/json" }, data: "{}" });
  expect(logout.status()).toBe(403);                                                              // a foreign page cannot sign you out either
  await bff(page, `/runs/${s.run}`, "PATCH", { title: "Seeded replay" });
});

test("the proxy refuses to reach anything but /api/v1", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password);
  for (const path of ["/../internal/runs", "/%2e%2e/internal/runs", "/runs/..%2f..%2finternal", "//internal", "/_blob/abc", "/ws/runs/x"]) {
    const r = await bff(page, path);
    expect([404, 400]).toContain(r.status);
  }
  const r = await bff<{ code: string }>(page, "/_blob/abc");
  expect(r.status).toBe(404);
});

test("the socket is read-only and only our origin may open it", async ({ page, browser }) => {
  const s = seed();
  await signIn(page, s.owner, s.password, `/runs/${s.run}`);
  const ticket = await bff<{ ticket: string; path: string }>(page, `/runs/${s.run}/stream-ticket`, "POST");
  expect(ticket.status).toBe(200);
  // our origin, an unexpected client message: closed with 1008
  const closed = await page.evaluate(({ api, path, ticket }) => new Promise<number>((resolve) => {
    const ws = new WebSocket(`${api.replace("http", "ws")}${path}?ticket=${ticket}`);
    ws.onopen = () => ws.send(JSON.stringify({ t: "subscribe", run: "other" }));
    ws.onclose = (e) => resolve(e.code);
    setTimeout(() => resolve(-1), 8000);
  }), { api: s.api, path: ticket.json.path, ticket: ticket.json.ticket });
  expect(closed).toBe(1008);
  // another origin (the same server through another hostname) is refused before it connects
  const ctx = await browser.newContext();
  const other = await ctx.newPage();
  await other.goto("http://127.0.0.1:3010/leaderboard");
  const t2 = await bff<{ ticket: string; path: string }>(page, `/runs/${s.run}/stream-ticket`, "POST");
  const refused = await other.evaluate(({ api, path, ticket }) => new Promise<string>((resolve) => {
    const ws = new WebSocket(`${api.replace("http", "ws")}${path}?ticket=${ticket}`);
    ws.onopen = () => resolve("open");
    ws.onclose = () => resolve("closed");
    setTimeout(() => resolve("timeout"), 8000);
  }), { api: s.api, path: t2.json.path, ticket: t2.json.ticket });
  expect(refused).toBe("closed");
  await ctx.close();
});

test("the key field is a password field with autocomplete off and is empty after a failed attempt", async ({ page }) => {
  const s = seed();
  await signIn(page, s.owner, s.password, "/runs/new");
  await waitIdle(page);
  for (let i = 0; i < 3; i++) await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Review" }).click();
  const key = page.getByLabel("Your provider API key");
  await expect(key).toHaveAttribute("type", "password");
  await expect(key).toHaveAttribute("autocomplete", "off");
  await key.fill(CANARY.replace("CANARY", "WRONG"));
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page.getByText("That provider key was not accepted")).toBeVisible();
  await expect(key).toHaveValue("");
  expect(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage, document.cookie]))).not.toContain("WRONG");
});
