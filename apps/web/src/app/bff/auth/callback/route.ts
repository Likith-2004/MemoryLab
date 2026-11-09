import { NextRequest, NextResponse } from "next/server";
import { webEnv } from "@/lib/env";
import { setSession } from "@/server/cookies";
import { csrfFailure, problem, opaqueFailure } from "@/server/guard";

/** The magic-link page reads the tokens GoTrue put in the URL fragment (a fragment never reaches a server), posts them here,
 *  and removes them from the address bar. They become httpOnly cookies only if the API accepts the access token. */
export const dynamic = "force-dynamic";

export async function POST(request: NextRequest): Promise<NextResponse> {
  try {
    return await post(request);
  } catch (error) {
    return opaqueFailure("callback", error);
  }
}

async function post(request: NextRequest): Promise<NextResponse> {
  const refused = csrfFailure(request);
  if (refused) return refused;
  let body: { access_token?: unknown; refresh_token?: unknown; expires_in?: unknown };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return problem(422, "validation_failed", "Invalid request");
  }
  const { access_token: access, refresh_token: refresh } = body;
  if (typeof access !== "string" || typeof refresh !== "string" || access.length > 4096 || refresh.length > 512) {
    return problem(422, "validation_failed", "Invalid request");
  }
  const check = await fetch(`${webEnv().apiUrl}/api/v1/me`, { headers: { authorization: `Bearer ${access}` }, cache: "no-store" }).catch(() => null);
  if (!check?.ok) return problem(401, "unauthenticated", "That sign-in link is no longer valid");
  const expiresIn = typeof body.expires_in === "number" ? body.expires_in : 900;
  const res = NextResponse.json({ ok: true }, { headers: { "cache-control": "no-store" } });
  setSession(res, { access, refresh, expiresIn });
  return res;
}
