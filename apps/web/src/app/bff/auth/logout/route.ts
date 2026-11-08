import { NextRequest, NextResponse } from "next/server";
import { clearSession } from "@/server/cookies";
import { csrfFailure, opaqueFailure } from "@/server/guard";
import { cookieNames, signOut } from "@/server/tokens";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest): Promise<NextResponse> {
  try {
    return await post(request);
  } catch (error) {
    return opaqueFailure("logout", error);
  }
}

async function post(request: NextRequest): Promise<NextResponse> {
  const refused = csrfFailure(request);
  if (refused) return refused;
  let everywhere = false;
  try {
    everywhere = ((await request.json()) as { everywhere?: unknown }).everywhere === true;
  } catch {
    /* no body: this device only */
  }
  const access = request.cookies.get(cookieNames().at)?.value;
  if (access) await signOut(access, everywhere);
  const res = NextResponse.json({ ok: true }, { headers: { "cache-control": "no-store" } });
  clearSession(res);
  return res;
}
