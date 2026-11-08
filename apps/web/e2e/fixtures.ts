import { expect, test as base, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

export const CANARY = "sk-ant-CANARY-0123456789abcdef";
export const XSS = JSON.parse(readFileSync(new URL("./xss-corpus.json", import.meta.url), "utf8")) as string[];
export const seed = () => JSON.parse(readFileSync(process.env.E2E_SEED_FILE ?? "/tmp/memorylab-e2e-seed.json", "utf8")) as {
  game: string; owner: string; admin: string; other: string; password: string; run: string; owner_id: string; admin_id: string; other_id: string; api: string;
};

/** Every test watches for what must never happen: a script running (the corpus sets window.__xss), a CSP violation, a JavaScript error or a dialog. */
export const test = base.extend<{ guard: void }>({
  guard: [async ({ page }, use) => {
    const problems: string[] = [];
    await page.addInitScript(() => {
      (window as unknown as { __csp: string[] }).__csp = [];
      document.addEventListener("securitypolicyviolation", (e) => (window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
    });
    page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
    page.on("dialog", async (d) => { problems.push(`dialog: ${d.message()}`); await d.dismiss(); });
    page.on("console", (m) => { if (m.type() === "error" && /Content Security Policy|Refused to/i.test(m.text())) problems.push(`console: ${m.text()}`); });
    await use();
    for (const p of page.context().pages()) {
      const csp = await p.evaluate(() => (window as unknown as { __csp?: string[] }).__csp ?? []).catch(() => []);
      const xss = await p.evaluate(() => (window as unknown as { __xss?: unknown }).__xss).catch(() => undefined);
      if (csp.length) problems.push(`csp violations: ${csp.join("; ")}`);
      if (xss !== undefined) problems.push(`script ran: __xss=${String(xss)}`);
    }
    expect(problems, problems.join("\n")).toEqual([]);
  }, { auto: true }],
});
export { expect };

export async function signIn(page: Page, email: string, password: string, next?: string): Promise<void> {
  await page.goto(`/auth/signin${next ? `?next=${encodeURIComponent(next)}` : ""}`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/auth/"));
}

/** A call through the web server's own proxy, as the page's JavaScript would make it. */
export async function bff<T>(page: Page, path: string, method = "GET", body?: unknown): Promise<{ status: number; json: T }> {
  return page.evaluate(async ({ path, method, body }) => {
    const r = await fetch(`/bff/api${path}`, { method, credentials: "same-origin",
      headers: { "content-type": "application/json", "x-memorylab-csrf": "1" }, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await r.text();
    let json: unknown = null;
    try { json = JSON.parse(text); } catch { json = text; }
    return { status: r.status, json: json as never };
  }, { path, method, body });
}

export async function control(path: string): Promise<void> {
  const r = await fetch(`${seed().api}/__e2e/${path}`, { method: "POST" });
  if (!r.ok) throw new Error(`control ${path} failed`);
}

/** One active run per account: wait until the previous test's run has ended before starting another. */
export async function waitIdle(page: Page): Promise<void> {
  await expect.poll(async () => (await bff<{ active_runs: number }>(page, "/me")).json.active_runs, { timeout: 45_000 }).toBe(0);
}
