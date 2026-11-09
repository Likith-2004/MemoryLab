import type { MetadataRoute } from "next";
import { sitemapEntries, siteUrl } from "@/lib/seo";
import type { LeaderRow } from "@/lib/types";
import { serverGet } from "@/server/viewer";

export const dynamic = "force-dynamic";

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const leaders = (await serverGet<LeaderRow[]>("/leaderboard?limit=100")) ?? [];
  return sitemapEntries(siteUrl(), leaders);
}
