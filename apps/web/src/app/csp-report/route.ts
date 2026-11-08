import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/** CSP violation reports. Only the violated directive and the blocked origin are logged: a blocked URL can carry a token. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const raw = await request.text();
  if (raw.length <= 8192) {
    try {
      const body = JSON.parse(raw) as { "csp-report"?: { "violated-directive"?: string; "blocked-uri"?: string } };
      const r = body["csp-report"];
      let blocked = "";
      try { blocked = new URL(r?.["blocked-uri"] ?? "").origin; } catch { blocked = String(r?.["blocked-uri"] ?? "").slice(0, 20); }
      console.warn(`csp-violation directive=${String(r?.["violated-directive"] ?? "").slice(0, 60)} blocked=${blocked}`);
    } catch { /* not a report */ }
  }
  return new NextResponse(null, { status: 204 });
}
