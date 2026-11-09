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

interface Entry { times: number[]; windowMs: number }
const hits = new Map<string, Entry>();
const MAX_KEYS = 10_000;

/** A small in-process sliding-window limiter. A key is dropped once its window is empty (swept every 256 calls), and the map is capped at
 *  MAX_KEYS with the least recently used key evicted, so a stream of distinct addresses or emails cannot grow it without bound. */
export function limited(key: string, limit: number, windowS: number, now = Date.now()): boolean {
  const windowMs = windowS * 1000;
  const recent = (hits.get(key)?.times ?? []).filter((t) => now - t < windowMs);
  hits.delete(key); // re-inserted below: Map order is insertion order, so the first key is the least recently used
  const refused = recent.length >= limit;
  if (!refused) recent.push(now);
  hits.set(key, { times: recent, windowMs });
  sweepCounter = (sweepCounter + 1) % 256;
  if (sweepCounter === 0) for (const [k, e] of hits) if (e.times.every((t) => now - t >= e.windowMs)) hits.delete(k);
  while (hits.size > MAX_KEYS) hits.delete(hits.keys().next().value as string);
  return refused;
}
let sweepCounter = 0;

export function limiterSize(): number {
  return hits.size;
}

/** Drops every key whose window has passed (also used by tests). */
export function sweepLimiter(now = Date.now()): void {
  for (const [k, e] of hits) if (e.times.every((t) => now - t >= e.windowMs)) hits.delete(k);
}
