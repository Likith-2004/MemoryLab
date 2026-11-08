import { cache } from "react";
import { cookies } from "next/headers";
import { webEnv } from "@/lib/env";
import type { Me } from "@/lib/types";
import { cookieNames } from "./tokens";

/** Who is looking, for server-rendered pages. The proxy has already refreshed an expiring session. */
export const getViewer = cache(async (): Promise<Me | null> => {
  const access = (await cookies()).get(cookieNames().at)?.value;
  if (!access) return null;
  try {
    const r = await fetch(`${webEnv().apiUrl}/api/v1/me`, { headers: { authorization: `Bearer ${access}` }, cache: "no-store" });
    return r.ok ? ((await r.json()) as Me) : null;
  } catch {
    return null;
  }
});

/** A server fetch of a public or (with the session) visible API resource. null on any non-200. */
export async function serverGet<T>(path: string): Promise<T | null> {
  const access = (await cookies()).get(cookieNames().at)?.value;
  try {
    const r = await fetch(`${webEnv().apiUrl}/api/v1${path}`, {
      headers: access ? { authorization: `Bearer ${access}` } : {},
      cache: "no-store",
    });
    return r.ok ? ((await r.json()) as T) : null;
  } catch {
    return null;
  }
}
