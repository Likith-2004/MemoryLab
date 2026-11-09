import type { MetadataRoute } from "next";
import { DISALLOWED, siteUrl } from "@/lib/seo";

export default function robots(): MetadataRoute.Robots {
  return { rules: [{ userAgent: "*", allow: "/", disallow: [...DISALLOWED] }], sitemap: `${siteUrl()}/sitemap.xml` };
}
