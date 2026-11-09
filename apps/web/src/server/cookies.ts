import type { NextResponse } from "next/server";
import { ACCESS_COOKIE_MAX_AGE, REFRESH_COOKIE_MAX_AGE, cookieNames, cookieOptions, type Tokens } from "./tokens";

export function setSession(res: NextResponse, t: Tokens): void {
  res.cookies.set(cookieNames().at, t.access, cookieOptions(Math.min(ACCESS_COOKIE_MAX_AGE, Math.max(t.expiresIn, 60) + 600)));
  res.cookies.set(cookieNames().rt, t.refresh, cookieOptions(REFRESH_COOKIE_MAX_AGE));
}

export function clearSession(res: NextResponse): void {
  res.cookies.set(cookieNames().at, "", { ...cookieOptions(0), maxAge: 0 });
  res.cookies.set(cookieNames().rt, "", { ...cookieOptions(0), maxAge: 0 });
}
