/** Browser-side API client. It talks only to this site's /bff/* routes (same origin); tokens are httpOnly cookies it cannot read. */

export class ApiProblem extends Error {
  constructor(public status: number, public code: string, public title: string, public retryAfter: number | null) {
    super(title);
  }
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? "GET";
  let res: Response;
  try {
    res = await fetch(`/bff/api${path}`, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers: { ...(init.body !== undefined ? { "content-type": "application/json" } : {}), "x-memorylab-csrf": "1" },
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
    });
  } catch {
    throw new ApiProblem(0, "network", "Cannot reach the server. Check your connection.", null);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let json: unknown = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    /* a non-JSON body (memory files, diffs) */
  }
  if (!res.ok) {
    const p = (json ?? {}) as { code?: string; title?: string };
    const retry = Number(res.headers.get("retry-after"));
    throw new ApiProblem(res.status, p.code ?? "internal", p.title ?? `Error ${res.status}`, Number.isFinite(retry) && retry > 0 ? retry : null);
  }
  return (json ?? text) as T;
}

export async function apiText(path: string): Promise<string> {
  const res = await fetch(`/bff/api${path}`, { credentials: "same-origin", cache: "no-store", headers: { "x-memorylab-csrf": "1" } });
  if (!res.ok) throw new ApiProblem(res.status, res.status === 404 ? "not_found" : "internal", `Error ${res.status}`, null);
  return res.text();
}

export async function session(path: "login" | "logout" | "callback", body: unknown): Promise<void> {
  const res = await fetch(`/bff/auth/${path}`, {
    method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "x-memorylab-csrf": "1" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    const p = (await res.json().catch(() => ({}))) as { code?: string; title?: string };
    throw new ApiProblem(res.status, p.code ?? "internal", p.title ?? "Request failed", Number(res.headers.get("retry-after")) || null);
  }
}

/** One user-facing sentence per error code (the screens never show raw server text for these). */
export function describe(e: unknown): string {
  if (!(e instanceof ApiProblem)) return "Something went wrong.";
  const when = e.retryAfter ? ` Try again in ${e.retryAfter >= 120 ? `${Math.round(e.retryAfter / 60)} minutes` : `${e.retryAfter} seconds`}.` : "";
  switch (e.code) {
    case "key_rejected": return "That provider key was not accepted. Check it and try again.";
    case "capacity_full": return `The lab is full right now.${when}`;
    case "breaker_open": return `New runs are paused by an operator.${when}`;
    case "rate_limited": return `Too many requests.${when}`;
    case "unauthenticated": return "Please sign in again.";
    case "not_found": return "Not found, or you may not see it.";
    case "network": return e.title;
    default: return e.status >= 500 ? "The server had a problem. Try again shortly." : e.title;
  }
}
