import { describe, expect, it } from "vitest";
import robots from "@/app/robots";
import { DISALLOWED, PUBLIC_PATHS, sitemapEntries, siteUrl } from "@/lib/seo";

describe("robots and sitemap", () => {
  it("point at the configured site and keep the account, admin and endpoint paths out", () => {
    process.env.MEMORYLAB_SITE_URL = "https://lab.example.org/";
    const r = robots();
    expect(r.sitemap).toBe("https://lab.example.org/sitemap.xml");
    const rule = Array.isArray(r.rules) ? r.rules[0]! : r.rules;
    expect(rule.disallow).toEqual([...DISALLOWED]);
    for (const p of ["/admin", "/settings", "/bff/", "/auth/", "/runs/new"]) expect(DISALLOWED as readonly string[]).toContain(p);
  });
  it("never blocks run pages: a private or unlisted run says noindex itself, and a crawler must be allowed to read that", () => {
    expect((DISALLOWED as readonly string[]).some((p) => p === "/runs" || p === "/runs/")).toBe(false);
  });
  it("lists the public pages and the leaderboard's runs, and nothing else", () => {
    const e = sitemapEntries("https://lab.example.org", [{ id: "run-1" }, { id: "a b" }]).map((x) => x.url);
    expect(e).toContain("https://lab.example.org");
    expect(e).toContain("https://lab.example.org/leaderboard");
    expect(e).toContain("https://lab.example.org/runs/run-1");
    expect(e).toContain("https://lab.example.org/runs/a%20b");
    expect(e).toHaveLength(PUBLIC_PATHS.length + 2);
    expect(e.some((u) => /\/(admin|settings|auth|bff)\b/.test(u))).toBe(false);
  });
  it("defaults the site to localhost and strips a trailing slash", () => {
    expect(siteUrl({})).toBe("http://localhost:3000");
    expect(siteUrl({ MEMORYLAB_SITE_URL: "https://x.org/" })).toBe("https://x.org");
  });
});
