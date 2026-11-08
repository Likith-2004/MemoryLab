import { NextRequest, NextResponse } from "next/server";
import { buildCsp, newNonce } from "@/lib/csp";
import { wsOrigin } from "@/lib/env";

/** Runs on every page request: a fresh nonce, the CSP, and the nonce handed to the renderer through a request header. */
export function proxy(request: NextRequest): NextResponse {
  const nonce = newNonce();
  const publicApi = (process.env.MEMORYLAB_PUBLIC_API_URL ?? process.env.MEMORYLAB_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
  const csp = buildCsp({
    nonce,
    dev: process.env.NODE_ENV === "development",
    apiOrigin: new URL(publicApi).origin,
    wsOrigin: wsOrigin(publicApi),
    reportUri: process.env.MEMORYLAB_CSP_REPORT_URI,
  });
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("content-security-policy", csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  matcher: [{ source: "/((?!_next/static|_next/image|favicon.ico|bff/).*)", missing: [{ type: "header", key: "next-router-prefetch" }] }],
};
