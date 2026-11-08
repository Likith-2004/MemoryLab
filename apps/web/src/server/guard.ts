import { NextRequest, NextResponse } from "next/server";
import { trustedHops, webEnv } from "@/lib/env";

export const CSRF_HEADER = "x-memorylab-csrf";

export function problem(status: number, code: string, title: string, headers: Record<string, string> = {}): NextResponse {
  return NextResponse.json({ type: "about:blank", title, status, code }, { status, headers: { "cache-control": "no-store", ...headers } });
}

/** An unexpected failure: log only an opaque id and the exception type (never request data: it may hold a provider key), and
 *  answer with the same id. */
export function opaqueFailure(where: string, error: unknown): NextResponse {
  const id = crypto.randomUUID().slice(0, 12);
  console.error(`web error id=${id} where=${where} type=${error instanceof Error ? error.name : "unknown"}`);
  return NextResponse.json({ type: "about:blank", title: "Internal error", status: 500, code: "internal", incident: id },
    { status: 500, headers: { "cache-control": "no-store" } });
}

/** Every non-GET request to this server's own routes must (a) come from our own origin (an exactly matching Origin, or, when
 *  the browser sends none, Sec-Fetch-Site: same-origin) and (b) carry a custom header, which forces a CORS preflight for any
 *  cross-origin caller. Anything else is a 403 before any work is done. GET and HEAD never change state. */
export function csrfFailure(request: NextRequest): NextResponse | null {
  if (request.method === "GET" || request.method === "HEAD") return null;
  const origin = request.headers.get("origin");
  const fetchSite = request.headers.get("sec-fetch-site");
  const siteOk = origin !== null ? origin === new URL(webEnv().siteUrl).origin : fetchSite === "same-origin";
  if (!siteOk || request.headers.get(CSRF_HEADER) !== "1") return problem(403, "forbidden", "Cross-site request refused");
  return null;
}

/** The client's address, from X-Forwarded-For, but only as far as we trust it. Next does not expose the socket peer and leaves a
 *  client-supplied X-Forwarded-For untouched (measured), so with no trusted load balancer in front (hops = 0) there is NO trustworthy
 *  source: null, and the API then sees this server as the peer. With N trusted load balancers the real peer is the entry the
 *  outermost one appended, N places from the right; everything a browser put to the left of it is ignored. */
export function clientAddress(request: NextRequest, hops = trustedHops()): string | null {
  if (hops < 1) return null;
  const chain = (request.headers.get("x-forwarded-for") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  if (chain.length < hops) return null;
  const entry = chain[chain.length - hops];
  return entry && /^[0-9a-fA-F:.]{2,45}$/.test(entry) ? entry : null;
}

const hits = new Map<string, number[]>();
/** A small in-process limiter for the login route (GoTrue has its own; this keeps one address from hammering it through us). */
export function limited(key: string, limit: number, windowS: number, now = Date.now()): boolean {
  const recent = (hits.get(key) ?? []).filter((t) => now - t < windowS * 1000);
  if (recent.length >= limit) {
    hits.set(key, recent);
    return true;
  }
  recent.push(now);
  hits.set(key, recent);
  return false;
}
