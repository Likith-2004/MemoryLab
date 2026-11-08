import { NextRequest, NextResponse } from "next/server";
import { clearSession, setSession } from "@/server/cookies";
import { clientAddress, csrfFailure, limited, problem, opaqueFailure } from "@/server/guard";
import { passwordLogin } from "@/server/tokens";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest): Promise<NextResponse> {
  try {
    return await post(request);
  } catch (error) {
    return opaqueFailure("login", error);
  }
}

async function post(request: NextRequest): Promise<NextResponse> {
  const refused = csrfFailure(request);
  if (refused) return refused;
  let body: { email?: unknown; password?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return problem(422, "validation_failed", "Invalid request");
  }
  if (typeof body.email !== "string" || typeof body.password !== "string" || body.email.length > 254 || body.password.length > 128) {
    return problem(422, "validation_failed", "Invalid request");
  }
  const email = body.email.trim().toLowerCase();
  const ip = clientAddress(request);
  // per target address (stops guessing one account) and, when we can trust one, per client address (stops spraying many)
  const perEmail = Number(process.env.MEMORYLAB_LOGIN_LIMIT_PER_MIN ?? "10") || 10; // the browser tests sign in far more often than a person
  if (limited(`login:email:${email}`, perEmail, 60) || (ip !== null && limited(`login:ip:${ip}`, perEmail * 2, 60))) {
    return problem(429, "rate_limited", "Too many attempts", { "retry-after": "60" });
  }
  const tokens = await passwordLogin(email, body.password);
  if (!tokens) {
    const res = problem(401, "unauthenticated", "Email or password is not right");
    clearSession(res);
    return res;
  }
  const res = NextResponse.json({ ok: true }, { headers: { "cache-control": "no-store" } });
  setSession(res, tokens);
  return res;
}
