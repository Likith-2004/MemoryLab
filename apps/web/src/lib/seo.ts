import type { LeaderRow } from "./types";

/** Pages a crawler may be pointed at. Run pages are listed only when public AND on the leaderboard (verified); private and unlisted runs
 *  are never in the sitemap and carry a noindex robots meta of their own (a Disallow here would hide that noindex from the crawler). */
export const PUBLIC_PATHS = ["/", "/leaderboard", "/experiments", "/privacy", "/terms", "/licences"] as const;

/** Paths with nothing for a search engine: the account and admin surfaces, the auth forms and the web server's own endpoints. */
export const DISALLOWED = ["/bff/", "/admin", "/auth/", "/settings", "/runs/new", "/csp-report"] as const;

export function siteUrl(env: Record<string, string | undefined> = process.env): string {
  return (env.MEMORYLAB_SITE_URL ?? "http://localhost:3000").replace(/\/$/, "");
}

export function sitemapEntries(site: string, leaders: Pick<LeaderRow, "id">[]): { url: string }[] {
  return [...PUBLIC_PATHS.map((p) => `${site}${p === "/" ? "" : p}`), ...leaders.map((l) => `${site}/runs/${encodeURIComponent(l.id)}`)].map((url) => ({ url }));
}
