import { NextRequest, NextResponse } from "next/server";
import { clientAddress, limited } from "@/server/guard";

export const dynamic = "force-dynamic";

const MAX_BYTES = 8192;

/** A log field can only hold plain characters: a newline in a hostile report must not be able to forge a separate log line. */
export function logSafe(value: unknown, max = 60): string {
  return String(value ?? "").slice(0, max).replace(/[^A-Za-z0-9 _:./'-]/g, "?");
}

/** Reads at most MAX_BYTES; null if the body is longer (the rest is never buffered). */
async function readCapped(request: NextRequest): Promise<string | null> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_BYTES || !request.body) return declared > MAX_BYTES ? null : "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BYTES) { await reader.cancel(); return null; }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

/** CSP violation reports: unauthenticated, so bounded in size and rate. Only the violated directive and the blocked origin are logged. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  if (limited(`csp:${clientAddress(request) ?? "server"}`, 60, 60)) return new NextResponse(null, { status: 429, headers: { "retry-after": "60" } });
  const raw = await readCapped(request);
  if (raw === null) return new NextResponse(null, { status: 413 });
  try {
    const body = JSON.parse(raw) as { "csp-report"?: { "violated-directive"?: unknown; "blocked-uri"?: unknown } };
    const r = body["csp-report"];
    let blocked: string;
    try { blocked = new URL(String(r?.["blocked-uri"] ?? "")).origin; } catch { blocked = String(r?.["blocked-uri"] ?? "").slice(0, 20); }
    // a directive is one known-shaped token and a blocked source an origin or keyword: anything else is logged as "other", so a hostile
    // report cannot put free text (a fake log line, a name, a token) in our log
    const directive = String(r?.["violated-directive"] ?? "").trim().split(/\s+/)[0] ?? "";
    console.warn(`csp-violation directive=${/^[a-z-]{3,40}$/.test(directive) ? directive : "other"} blocked=${/^[A-Za-z0-9:./-]{1,80}$/.test(blocked) ? blocked : "other"}`);
  } catch { /* not a report */ }
  return new NextResponse(null, { status: 204 });
}
