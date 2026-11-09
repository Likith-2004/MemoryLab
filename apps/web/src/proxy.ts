import { NextRequest, NextResponse } from "next/server";
import { buildCsp, newNonce } from "@/lib/csp";
import { webEnv, wsOrigin } from "@/lib/env";
import { clearSession, setSession } from "@/server/cookies";
import { cookieNames, needsRefresh, refreshTokens } from "@/server/tokens";

/** Runs on every page request: a fresh nonce, the CSP, and the nonce handed to the renderer through a request header. */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const nonce = newNonce();
  const publicApi = webEnv().publicApiUrl;
  const csp = buildCsp({
    nonce,
    dev: process.env.NODE_ENV === "development",
    apiOrigin: new URL(publicApi).origin,
    wsOrigin: wsOrigin(publicApi),
    reportUri: process.env.MEMORYLAB_CSP_REPORT_URI,
    upgrade: webEnv().siteUrl.startsWith("https://"),
  });
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("content-security-policy", csp);
  // Keep the session fresh for page renders: a server component cannot set cookies, so the refresh happens here.
  const names = cookieNames();
  const refresh = request.cookies.get(names.rt)?.value;
  const access = request.cookies.get(names.at)?.value;
  let rotated: Awaited<ReturnType<typeof refreshTokens>> = null;
  let dead = false;
  if (refresh && needsRefresh(access)) {
    rotated = await refreshTokens(refresh);
    dead = !rotated;
    const jar = new Map(request.cookies.getAll().map((c) => [c.name, c.value]));
    if (rotated) {
      jar.set(names.at, rotated.access);
      jar.set(names.rt, rotated.refresh);
    } else {
      jar.delete(names.at);
      jar.delete(names.rt);
    }
    headers.set("cookie", [...jar].map(([k, v]) => `${k}=${v}`).join("; "));
  }
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("content-security-policy", csp);
  if (rotated) setSession(response, rotated);
  else if (dead) clearSession(response);
  return response;
}

export const config = {
  matcher: [{ source: "/((?!_next/static|_next/image|favicon.ico|bff/).*)", missing: [{ type: "header", key: "next-router-prefetch" }] }],
};
