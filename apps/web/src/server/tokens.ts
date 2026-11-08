/** Session tokens live only in httpOnly cookies and only the web server reads them: the browser's JavaScript never sees an access
 *  or refresh token. This module has no Next imports so the proxy (page requests) and the route handlers can share it. */
import { webEnv } from "@/lib/env";

/** `__Host-` cookies (Secure, Path=/, no Domain) wherever the site is https; plain names only for http://localhost development. */
export function cookieNames(): { at: string; rt: string } {
  const secure = webEnv().siteUrl.startsWith("https://");
  return secure ? { at: "__Host-ml_at", rt: "__Host-ml_rt" } : { at: "ml_at", rt: "ml_rt" };
}
export const REFRESH_SKEW_S = 60;

export interface Tokens {
  access: string;
  refresh: string;
  expiresIn: number;
}

export function jwtExp(token: string): number {
  try {
    const part = token.split(".")[1] ?? "";
    const payload = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/"))) as { exp?: number };
    return typeof payload.exp === "number" ? payload.exp : 0;
  } catch {
    return 0;
  }
}

export function needsRefresh(access: string | undefined, nowS = Date.now() / 1000): boolean {
  return !access || jwtExp(access) - REFRESH_SKEW_S <= nowS;
}

export function cookieOptions(maxAgeS: number) {
  const secure = webEnv().siteUrl.startsWith("https://");
  return { httpOnly: true, secure, sameSite: "strict" as const, path: "/", maxAge: maxAgeS };
}

export const ACCESS_COOKIE_MAX_AGE = 3600;
export const REFRESH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30;

// A refresh token is single use (it rotates), so concurrent requests carrying the same one must share one refresh.
const inflight = new Map<string, Promise<Tokens | null>>();

export function refreshTokens(refresh: string): Promise<Tokens | null> {
  const running = inflight.get(refresh);
  if (running) return running;
  const job = doRefresh(refresh).finally(() => setTimeout(() => inflight.delete(refresh), 5000));
  inflight.set(refresh, job);
  return job;
}

async function doRefresh(refresh: string): Promise<Tokens | null> {
  const env = webEnv();
  try {
    const r = await fetch(`${env.authUrl}/token?grant_type=refresh_token`, {
      method: "POST",
      headers: { apikey: env.publishableKey, "content-type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
      cache: "no-store",
    });
    if (!r.ok) return null;
    const body = (await r.json()) as { access_token?: string; refresh_token?: string; expires_in?: number };
    if (!body.access_token || !body.refresh_token) return null;
    return { access: body.access_token, refresh: body.refresh_token, expiresIn: body.expires_in ?? 900 };
  } catch {
    return null;
  }
}

export async function passwordLogin(email: string, password: string): Promise<Tokens | null> {
  const env = webEnv();
  try {
    const r = await fetch(`${env.authUrl}/token?grant_type=password`, {
      method: "POST",
      headers: { apikey: env.publishableKey, "content-type": "application/json" },
      body: JSON.stringify({ email, password }),
      cache: "no-store",
    });
    if (!r.ok) return null; // wrong password, unknown address and unconfirmed address are one answer
    const body = (await r.json()) as { access_token?: string; refresh_token?: string; expires_in?: number };
    if (!body.access_token || !body.refresh_token) return null;
    return { access: body.access_token, refresh: body.refresh_token, expiresIn: body.expires_in ?? 900 };
  } catch {
    return null;
  }
}

export async function signOut(access: string, everywhere: boolean): Promise<void> {
  const env = webEnv();
  try {
    await fetch(`${env.authUrl}/logout?scope=${everywhere ? "global" : "local"}`, {
      method: "POST",
      headers: { apikey: env.publishableKey, authorization: `Bearer ${access}` },
      cache: "no-store",
    });
  } catch {
    /* the cookies are cleared either way */
  }
}
