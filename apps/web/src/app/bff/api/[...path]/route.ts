import { NextRequest, NextResponse } from "next/server";
import { webEnv } from "@/lib/env";
import { clearSession, setSession } from "@/server/cookies";
import { clientAddress, csrfFailure, opaqueFailure, problem } from "@/server/guard";
import { cookieNames, needsRefresh, refreshTokens, type Tokens } from "@/server/tokens";

/** The browser talks to /bff/api/*; this adds the bearer token from the httpOnly cookie and forwards to /api/v1/* and to nothing else.
 *  Expired tokens are refreshed here (once, and once more after a 401). Redirects are never followed: a frame is a 302 to a signed
 *  URL that the browser fetches itself. The request body (it may carry a provider key) is forwarded as bytes: never parsed or logged. */

export const dynamic = "force-dynamic";

const SEGMENT = /^[A-Za-z0-9_.@:-]{1,128}$/;
const MAX_BODY = 64 * 1024;
const PASS = ["content-type", "retry-after", "location", "x-frame-step", "content-disposition", "x-content-type-options"];
const BAD_RAW_PATH = /(%2f|%5c|%2e|%00|\/\/|\\)/i;

/** `/bff/api/runs/{id}/memory` -> `runs/{id}/memory`, or null when anything about the path is not plainly a normal API path. */
export function upstreamPath(rawPathname: string, segments: string[]): string | null {
  const prefix = "/bff/api/";
  if (!rawPathname.startsWith(prefix) || BAD_RAW_PATH.test(rawPathname.slice(prefix.length - 1))) return null;
  if (!segments.length || segments.some((s) => s === ".." || s === "." || !SEGMENT.test(s))) return null;
  return segments.join("/");
}

async function handle(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }): Promise<NextResponse> {
  try {
    const refused = csrfFailure(request);
    if (refused) return refused;
    const { path } = await ctx.params;
    const rel = upstreamPath(request.nextUrl.pathname, path);
    if (rel === null) return problem(404, "not_found", "Not found");
    const env = webEnv();
    const url = `${env.apiUrl}/api/v1/${rel}${request.nextUrl.search}`;

    let body: ArrayBuffer | undefined;
    if (request.method !== "GET" && request.method !== "HEAD") {
      const declared = Number(request.headers.get("content-length") ?? "0");
      if (declared > MAX_BODY) return problem(413, "payload_too_large", "Payload too large");
      body = await request.arrayBuffer();
      if (body.byteLength > MAX_BODY) return problem(413, "payload_too_large", "Payload too large");
    }
    const names = cookieNames();
    let access = request.cookies.get(names.at)?.value;
    let refresh = request.cookies.get(names.rt)?.value;
    let rotated: Tokens | null = null;
    let cleared = false;

    const refreshNow = async (): Promise<boolean> => {
      if (!refresh) return false;
      const t = await refreshTokens(refresh);
      if (!t) {
        cleared = true;
        access = undefined;
        refresh = undefined;
        return false;
      }
      rotated = t;
      access = t.access;
      refresh = t.refresh;
      return true;
    };
    if (refresh && needsRefresh(access)) await refreshNow();

    const peer = clientAddress(request);
    const send = (): Promise<Response> => {
      // Only these headers are ever sent upstream: never the browser's Authorization, Cookie or X-Forwarded-*.
      const headers: Record<string, string> = {};
      const contentType = request.headers.get("content-type");
      if (contentType && contentType.length < 100) headers["content-type"] = contentType;
      if (access) headers.authorization = `Bearer ${access}`;
      if (peer) headers["x-forwarded-for"] = peer;
      return fetch(url, { method: request.method, headers, body, redirect: "manual", cache: "no-store" });
    };
    let upstream: Response;
    try {
      upstream = await send();
      if (upstream.status === 401 && refresh && !rotated) {
        if (await refreshNow()) upstream = await send();
      }
    } catch {
      return problem(502, "internal", "The service is not reachable");
    }

    const out = new Headers({ "cache-control": "no-store" });
    for (const name of PASS) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    const res = new NextResponse(upstream.status === 204 || upstream.status === 304 ? null : upstream.body, { status: upstream.status, headers: out });
    if (rotated) setSession(res, rotated);
    else if (cleared) clearSession(res);
    return res;
  } catch (error) {
    return opaqueFailure("proxy", error);
  }
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const DELETE = handle;
