import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { GET as proxyGet, POST as proxyPost, DELETE as proxyDelete, upstreamPath } from "@/app/bff/api/[...path]/route";
import { POST as login } from "@/app/bff/auth/login/route";
import { POST as logout } from "@/app/bff/auth/logout/route";
import { POST as callback } from "@/app/bff/auth/callback/route";
import { clientAddress } from "@/server/guard";
import { productionProblems } from "@/lib/env";
import { jwtExp, needsRefresh } from "@/server/tokens";

const SITE = "http://localhost:3000";
const CANARY = "sk-ant-CANARY-" + "0123456789abcdef";

function jwt(exp: number, role = "authenticated"): string {
  const b = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b({ alg: "none" })}.${b({ exp, role })}.sig`;
}
const future = () => Math.floor(Date.now() / 1000) + 600;

interface Call { url: string; init: RequestInit }
let calls: Call[];
let upstream: (url: string, init: RequestInit) => Response;

beforeEach(() => {
  process.env.MEMORYLAB_PUBLISHABLE_KEY = "pk-test";
  process.env.MEMORYLAB_SITE_URL = SITE;
  process.env.MEMORYLAB_API_URL = "http://api.internal:8000";
  process.env.MEMORYLAB_AUTH_URL = "http://auth.internal/auth/v1";
  delete process.env.MEMORYLAB_WEB_TRUSTED_HOPS;
  calls = [];
  upstream = () => new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    return upstream(String(url), init);
  }));
});
afterEach(() => vi.unstubAllGlobals());

function req(path: string, init: { method?: string; headers?: Record<string, string>; body?: string; origin?: string | null } = {}): NextRequest {
  const headers: Record<string, string> = { "x-memorylab-csrf": "1", ...(init.headers ?? {}) };
  if (init.origin !== null) headers.origin = init.origin ?? SITE;
  return new NextRequest(`${SITE}${path}`, { method: init.method ?? "GET", headers, body: init.body });
}
const ctx = (path: string) => ({ params: Promise.resolve({ path: path.split("/").filter(Boolean) }) });
const session = (access: string, refresh = "rt-1") => ({ cookie: `ml_at=${access}; ml_rt=${refresh}` });

describe("CSRF: every state-changing request must be same-origin and carry the custom header", () => {
  const cases: [string, Record<string, string>, string | null | undefined, number][] = [
    ["foreign Origin", {}, "https://evil.example", 403],
    ["null Origin", {}, "null", 403],
    ["no Origin and no Sec-Fetch-Site", {}, null, 403],
    ["no Origin, Sec-Fetch-Site cross-site", { "sec-fetch-site": "cross-site" }, null, 403],
    ["no Origin, Sec-Fetch-Site same-site (a sibling subdomain)", { "sec-fetch-site": "same-site" }, null, 403],
    ["Origin that merely starts with ours", {}, "http://localhost:3000.evil.example", 403],
    ["same Origin, header missing", { "x-memorylab-csrf": "" }, undefined, 403],
    ["same Origin and header", {}, undefined, 200],
    ["no Origin but Sec-Fetch-Site same-origin", { "sec-fetch-site": "same-origin" }, null, 200],
  ];
  for (const [name, headers, origin, status] of cases) {
    it(`${name} -> ${status}`, async () => {
      const r = await proxyPost(req("/bff/api/runs/validate", { method: "POST", headers, origin, body: "{}" }), ctx("runs/validate"));
      expect(r.status).toBe(status);
      if (status === 403) expect(calls).toEqual([]); // refused before any work
    });
  }
  it("the session routes apply the same rule", async () => {
    for (const handler of [login, logout, callback]) {
      const r = await handler(req("/bff/auth/x", { method: "POST", origin: "https://evil.example", body: "{}" }));
      expect(r.status).toBe(403);
    }
    expect(calls).toEqual([]);
  });
  it("GET never needs the header", async () => {
    const r = await proxyGet(req("/bff/api/games", { headers: { "x-memorylab-csrf": "" } }), ctx("games"));
    expect(r.status).toBe(200);
  });
});

describe("proxy allowlist", () => {
  it("accepts ordinary API paths", () => {
    expect(upstreamPath("/bff/api/runs/abc-123/memory/file", ["runs", "abc-123", "memory", "file"])).toBe("runs/abc-123/memory/file");
  });
  const bad: [string, string, string[]][] = [
    ["dot-dot", "/bff/api/runs/../../internal/runs", ["runs", "..", "..", "internal", "runs"]],
    ["encoded dot-dot", "/bff/api/runs/%2e%2e/x", ["runs", "..", "x"]],
    ["encoded slash", "/bff/api/runs%2f..%2finternal", ["runs/../internal"]],
    ["encoded backslash", "/bff/api/runs%5cx", ["runs\\x"]],
    ["double slash", "/bff/api/runs//x", ["runs", "x"]],
    ["NUL", "/bff/api/runs/%00", ["runs", "\u0000"]],
    ["empty", "/bff/api/", []],
    ["space", "/bff/api/a b", ["a b"]],
    ["wrong prefix", "/other/runs", ["runs"]],
  ];
  for (const [name, raw, segs] of bad) it(`rejects ${name}`, () => expect(upstreamPath(raw, segs)).toBeNull());

  it("only ever forwards to /api/v1 on the API host", async () => {
    await proxyGet(req("/bff/api/games?x=1"), ctx("games"));
    expect(calls.map((c) => c.url)).toEqual(["http://api.internal:8000/api/v1/games?x=1"]);
    for (const p of ["_blob/abc", "internal/runs", "ws/runs/x"]) {
      calls = [];
      await proxyGet(req(`/bff/api/${p}`), ctx(p)); // not an /api/v1 path: it becomes /api/v1/_blob/... which the API does not serve
      expect(calls.every((c) => c.url.startsWith("http://api.internal:8000/api/v1/"))).toBe(true);
    }
  });
  it("a traversal attempt never reaches the network", async () => {
    const r = await proxyGet(req("/bff/api/runs/..%2f..%2finternal/runs"), ctx("runs/..%2f..%2finternal/runs"));
    expect(r.status).toBe(404);
    expect(calls).toEqual([]);
  });
});

describe("what is sent upstream", () => {
  it("drops the browser's Authorization, Cookie and X-Forwarded-* and sets its own", async () => {
    process.env.MEMORYLAB_WEB_TRUSTED_HOPS = "1";
    await proxyPost(req("/bff/api/runs", {
      method: "POST", body: "{}",
      headers: { authorization: "Bearer attacker", cookie: "ml_at=" + jwt(future()) + "; other=1", "x-forwarded-for": "6.6.6.6, 10.0.0.9", "x-forwarded-host": "evil",
        "x-real-ip": "6.6.6.6", forwarded: "for=6.6.6.6", "content-type": "application/json" },
    }), ctx("runs"));
    const sent = new Headers(calls[0]!.init.headers);
    expect(sent.get("authorization")).toBe("Bearer " + jwt(future()).replace(/\.[^.]+$/, ".sig"));
    for (const h of ["cookie", "x-forwarded-host", "x-real-ip", "forwarded"]) expect(sent.get(h)).toBeNull();
    expect(sent.get("x-forwarded-for")).toBe("10.0.0.9"); // the entry our load balancer appended, not the browser's claim
  });
  it("a spoofed X-Forwarded-For never becomes the API's client address", () => {
    const r = (xff: string) => new NextRequest(SITE + "/", { headers: { "x-forwarded-for": xff } });
    expect(clientAddress(r("6.6.6.6"), 0)).toBeNull();                              // no trusted LB: a header alone proves nothing
    expect(clientAddress(r("6.6.6.6, 203.0.113.9"), 1)).toBe("203.0.113.9");        // the entry our one LB appended, not the claim
    expect(clientAddress(r("6.6.6.6, 203.0.113.9, 10.0.0.1"), 2)).toBe("203.0.113.9"); // two trusted LBs
    expect(clientAddress(r("6.6.6.6"), 2)).toBeNull();                              // fewer entries than hops: not from our LBs
    expect(clientAddress(r("evil, <script>"), 1)).toBeNull();
    expect(clientAddress(new NextRequest(SITE + "/"), 1)).toBeNull();
  });
  it("caps the body at 64 KB without reading it all", async () => {
    const big = "x".repeat(70 * 1024);
    const r = await proxyPost(req("/bff/api/runs", { method: "POST", body: big, headers: { "content-length": String(big.length) } }), ctx("runs"));
    expect(r.status).toBe(413);
    expect(calls).toEqual([]);
  });
  it("caps a body that declares no length (chunked) after reading it, without forwarding it", async () => {
    const big = "x".repeat(70 * 1024);
    const r = await proxyPost(req("/bff/api/runs", { method: "POST", body: big }), ctx("runs"));   // no content-length header
    expect(r.status).toBe(413);
    expect(calls).toEqual([]);
  });
  it("never follows redirects and passes a 302 straight to the browser", async () => {
    upstream = () => new Response(null, { status: 302, headers: { location: "http://api.example/_blob/x?sig=1", "x-frame-step": "10", "set-cookie": "x=1", server: "uvicorn" } });
    const r = await proxyGet(req("/bff/api/runs/r1/frames/12"), ctx("runs/r1/frames/12"));
    expect(calls[0]!.init.redirect).toBe("manual");
    expect(r.status).toBe(302);
    expect(r.headers.get("location")).toBe("http://api.example/_blob/x?sig=1");
    expect(r.headers.get("x-frame-step")).toBe("10");
    expect(r.headers.get("set-cookie")).toBeNull();
    expect(r.headers.get("server")).toBeNull();
  });
  it("is never cached", async () => {
    const r = await proxyGet(req("/bff/api/games"), ctx("games"));
    expect(r.headers.get("cache-control")).toBe("no-store");
    expect(calls[0]!.init.cache).toBe("no-store");
    const route = await import("@/app/bff/api/[...path]/route");
    expect(route.dynamic).toBe("force-dynamic");
  });
  it("sends no credentials when there is no session", async () => {
    await proxyGet(req("/bff/api/games"), ctx("games"));
    expect(new Headers(calls[0]!.init.headers).get("authorization")).toBeNull();
  });
});

describe("the provider key passes through but is never logged or stored", () => {
  it("a canary key in a run-creation body reaches the API only, and appears in no log, header, response or cookie", async () => {
    const logs: string[] = [];
    for (const m of ["log", "info", "warn", "error", "debug"] as const) vi.spyOn(console, m).mockImplementation((...a) => { logs.push(a.map(String).join(" ")); });
    const body = JSON.stringify({ game: "g", api_key: CANARY });
    upstream = () => new Response(JSON.stringify({ code: "key_rejected", title: "The key was not accepted" }), { status: 422, headers: { "content-type": "application/json" } });
    const r = await proxyPost(req("/bff/api/runs", { method: "POST", body, headers: { ...session(jwt(future())), "content-type": "application/json" } }), ctx("runs"));
    expect(Buffer.from(calls[0]!.init.body as ArrayBuffer).toString()).toContain(CANARY);            // forwarded as bytes
    expect(await r.text()).not.toContain(CANARY);
    expect([...r.headers.values()].join("\n")).not.toContain(CANARY);
    expect(logs.join("\n")).not.toContain(CANARY);
  });
  it("an exception returns only an opaque incident id and logs only the id and the exception type", async () => {
    const logs: string[] = [];
    vi.spyOn(console, "error").mockImplementation((...a) => { logs.push(a.map(String).join(" ")); });
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error(`boom ${CANARY}`); }));
    // the upstream failure is caught as 502; force an error outside it by breaking the request body read
    const broken = req("/bff/api/runs", { method: "POST", body: "{}" });
    vi.spyOn(broken, "arrayBuffer").mockRejectedValue(new Error(`read failed ${CANARY}`));
    const r = await proxyPost(broken, ctx("runs"));
    const text = await r.text();
    expect(r.status).toBe(500);
    expect(JSON.parse(text)).toMatchObject({ code: "internal", title: "Internal error" });
    expect(JSON.parse(text).incident).toMatch(/^[0-9a-f-]{12}$/);
    expect(text).not.toContain(CANARY);
    expect(logs.join("\n")).not.toContain(CANARY);
    expect(logs.join("\n")).toMatch(/web error id=[0-9a-f-]{12} where=proxy type=Error/);
  });
  it("an unreachable API is an opaque 502", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error(`connect ECONNREFUSED ${CANARY}`); }));
    const r = await proxyPost(req("/bff/api/runs", { method: "POST", body: JSON.stringify({ api_key: CANARY }) }), ctx("runs"));
    expect(r.status).toBe(502);
    expect(await r.text()).not.toContain(CANARY);
  });
});

describe("session handling", () => {
  it("login sets httpOnly SameSite=Strict cookies and returns no token", async () => {
    upstream = () => new Response(JSON.stringify({ access_token: jwt(future()), refresh_token: "rt-new", expires_in: 900 }), { status: 200 });
    const r = await login(req("/bff/auth/login", { method: "POST", body: JSON.stringify({ email: "A@Example.test", password: "pw-pw-pw-pw-1" }) }));
    expect(r.status).toBe(200);
    const setCookie = r.headers.getSetCookie().join("\n");
    expect(setCookie).toMatch(/ml_at=/);
    expect(setCookie).toMatch(/ml_rt=rt-new/);
    for (const c of r.headers.getSetCookie()) {
      expect(c).toMatch(/HttpOnly/i);
      expect(c).toMatch(/SameSite=strict/i);
      expect(c).toMatch(/Path=\//);
    }
    expect(await r.text()).not.toMatch(/rt-new|eyJ/);
    expect(JSON.parse(String(calls[0]!.init.body)).email).toBe("a@example.test");
    expect(new Headers(calls[0]!.init.headers).get("apikey")).toBe("pk-test");
    expect(calls[0]!.url).toBe("http://auth.internal/auth/v1/token?grant_type=password");
  });
  it("secure sites get __Host- cookies with Secure", async () => {
    process.env.MEMORYLAB_SITE_URL = "https://lab.example.org";
    upstream = () => new Response(JSON.stringify({ access_token: jwt(future()), refresh_token: "rt-new", expires_in: 900 }), { status: 200 });
    const r = await login(req("/bff/auth/login", { method: "POST", origin: "https://lab.example.org", body: JSON.stringify({ email: "a@example.test", password: "pw-pw-pw-pw-1" }) }));
    const cookies = r.headers.getSetCookie();
    expect(cookies.length).toBe(2);
    for (const c of cookies) {
      expect(c).toMatch(/^__Host-ml_(at|rt)=/);
      expect(c).toMatch(/; Secure/i);
      expect(c).toMatch(/HttpOnly/i);
      expect(c).toMatch(/SameSite=strict/i);
      expect(c).toMatch(/Path=\//);
      expect(c).not.toMatch(/Domain=/i);
    }
  });
  it("a wrong, unknown or unconfirmed login is one answer and sets no cookie", async () => {
    upstream = () => new Response(JSON.stringify({ error_code: "email_not_confirmed" }), { status: 400 });
    const r = await login(req("/bff/auth/login", { method: "POST", body: JSON.stringify({ email: "a@example.test", password: "pw-pw-pw-pw-1" }) }));
    expect(r.status).toBe(401);
    expect(JSON.parse(await r.text()).code).toBe("unauthenticated");
    expect(r.headers.getSetCookie().join()).not.toMatch(/ml_at=[^;]/);
  });
  it("login is rate limited per target address, and per client address once a trusted one is known", async () => {
    upstream = () => new Response("{}", { status: 400 });
    const codes: number[] = [];
    for (let i = 0; i < 12; i++) codes.push((await login(req("/bff/auth/login", { method: "POST", body: JSON.stringify({ email: "guessme@example.test", password: "x" }) }))).status);
    expect(codes.slice(0, 10).every((c) => c === 401)).toBe(true);
    expect(codes.slice(10)).toEqual([429, 429]);
    process.env.MEMORYLAB_WEB_TRUSTED_HOPS = "1";
    const spray: number[] = [];
    for (let i = 0; i < 22; i++) spray.push((await login(req("/bff/auth/login", { method: "POST", headers: { "x-forwarded-for": "198.51.100.77" }, body: JSON.stringify({ email: `u${i}@example.test`, password: "x" }) }))).status);
    expect(spray.slice(0, 20).every((c) => c === 401) && spray.slice(20).every((c) => c === 429)).toBe(true);
  });
  it("login validates its input before calling anything", async () => {
    for (const body of ['{"email":1,"password":"x"}', "not json", '{"email":"a@b.co"}', JSON.stringify({ email: "a@b.co", password: "x".repeat(200) })]) {
      const r = await login(req("/bff/auth/login", { method: "POST", headers: { "x-forwarded-for": "198.51.100." + Math.floor(Math.random() * 200) }, body }));
      expect(r.status).toBe(422);
    }
    expect(calls).toEqual([]);
  });
  it("an expiring access token is refreshed before the call and the rotated tokens are stored back in cookies", async () => {
    const fresh = jwt(future());
    upstream = (url) => url.includes("/token?grant_type=refresh_token")
      ? new Response(JSON.stringify({ access_token: fresh, refresh_token: "rt-2", expires_in: 900 }), { status: 200 })
      : new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    const r = await proxyGet(req("/bff/api/me", { headers: session(jwt(Math.floor(Date.now() / 1000) + 10), "rt-1") }), ctx("me"));
    expect(calls.map((c) => c.url)).toEqual(["http://auth.internal/auth/v1/token?grant_type=refresh_token", "http://api.internal:8000/api/v1/me"]);
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ refresh_token: "rt-1" });
    expect(new Headers(calls[1]!.init.headers).get("authorization")).toBe(`Bearer ${fresh}`);
    expect(r.headers.getSetCookie().join("\n")).toMatch(/ml_rt=rt-2/);
  });
  it("a 401 triggers one refresh and one retry", async () => {
    let apiCalls = 0;
    upstream = (url) => {
      if (url.includes("grant_type=refresh_token")) return new Response(JSON.stringify({ access_token: jwt(future()), refresh_token: "rt-2", expires_in: 900 }), { status: 200 });
      return ++apiCalls === 1 ? new Response("{}", { status: 401 }) : new Response("{}", { status: 200 });
    };
    const r = await proxyGet(req("/bff/api/me", { headers: session(jwt(future()), "rt-1") }), ctx("me"));
    expect(r.status).toBe(200);
    expect(apiCalls).toBe(2);
  });
  it("a dead refresh token clears both cookies", async () => {
    upstream = (url) => url.includes("grant_type=refresh_token") ? new Response("{}", { status: 400 }) : new Response("{}", { status: 401 });
    const r = await proxyGet(req("/bff/api/me", { headers: session(jwt(Math.floor(Date.now() / 1000) - 5), "dead") }), ctx("me"));
    const cleared = r.headers.getSetCookie().join("\n");
    expect(cleared).toMatch(/ml_at=;/);
    expect(cleared).toMatch(/ml_rt=;/);
  });
  it("concurrent requests with one refresh token share a single refresh", async () => {
    let refreshes = 0;
    upstream = (url) => {
      if (url.includes("grant_type=refresh_token")) { refreshes++; return new Response(JSON.stringify({ access_token: jwt(future()), refresh_token: "rt-2", expires_in: 900 }), { status: 200 }); }
      return new Response("{}", { status: 200 });
    };
    const expired = jwt(Math.floor(Date.now() / 1000) - 5);
    await Promise.all([1, 2, 3].map(() => proxyGet(req("/bff/api/me", { headers: session(expired, "rt-shared") }), ctx("me"))));
    expect(refreshes).toBe(1);
  });
  it("logout calls GoTrue's logout with the right scope and clears the cookies", async () => {
    for (const [everywhere, scope] of [[false, "local"], [true, "global"]] as const) {
      calls = [];
      const r = await logout(req("/bff/auth/logout", { method: "POST", body: JSON.stringify({ everywhere }), headers: session(jwt(future())) }));
      expect(r.status).toBe(200);
      expect(calls[0]!.url).toBe(`http://auth.internal/auth/v1/logout?scope=${scope}`);
      expect(new Headers(calls[0]!.init.headers).get("authorization")).toMatch(/^Bearer /);
      expect(r.headers.getSetCookie().join("\n")).toMatch(/ml_at=;/);
    }
  });
  it("the magic-link callback keeps tokens only if the API accepts the access token", async () => {
    const body = JSON.stringify({ access_token: jwt(future()), refresh_token: "rt-m", expires_in: 900 });
    upstream = () => new Response("{}", { status: 401 });
    let r = await callback(req("/bff/auth/callback", { method: "POST", body }));
    expect(r.status).toBe(401);
    expect(r.headers.getSetCookie()).toEqual([]);
    upstream = () => new Response("{}", { status: 200 });
    r = await callback(req("/bff/auth/callback", { method: "POST", body }));
    expect(r.status).toBe(200);
    expect(r.headers.getSetCookie().join()).toMatch(/ml_rt=rt-m/);
  });
});

describe("production configuration guard", () => {
  const ok = { apiUrl: "https://api.example.org", authUrl: "https://auth.example.org/auth/v1", publicApiUrl: "https://api.example.org", siteUrl: "https://lab.example.org" };
  it("accepts https everywhere and loopback http", () => {
    expect(productionProblems(ok)).toEqual([]);
    expect(productionProblems({ apiUrl: "http://127.0.0.1:8000", authUrl: "http://localhost:54321/auth/v1", publicApiUrl: "http://127.0.0.1:8000", siteUrl: "http://localhost:3000" })).toEqual([]);
  });
  it("names every plain-http remote URL", () => {
    const problems = productionProblems({ ...ok, apiUrl: "http://api.internal:8000", publicApiUrl: "http://api.example.org", siteUrl: "http://lab.example.org" });
    expect(problems).toHaveLength(3);
    expect(problems.join()).toMatch(/apiUrl.*publicApiUrl.*siteUrl|publicApiUrl/s);
  });
  it("allows plain http to a listed internal host for the server-side URLs only", () => {
    const e = { ...ok, apiUrl: "http://api:8000", authUrl: "http://kong:8000/auth/v1" };
    expect(productionProblems(e)).toHaveLength(2);
    expect(productionProblems(e, ["api", "kong"])).toEqual([]);
    expect(productionProblems(e, ["API", "kong"])).toEqual([]);                                          // hostnames are case-insensitive
    expect(productionProblems({ ...e, publicApiUrl: "http://api:8000" }, ["api", "kong"])).toHaveLength(1);  // never for what a browser sees
    expect(productionProblems({ ...e, siteUrl: "http://api:3000" }, ["api", "kong"])).toHaveLength(1);
    expect(productionProblems({ ...e, apiUrl: "http://api.evil.example:8000" }, ["api", "kong"])).toHaveLength(1);   // exact match, not a suffix
  });
  it("webEnv refuses to start in production with a plain-http remote origin", async () => {
    const { webEnv } = await import("@/lib/env");
    vi.stubEnv("NODE_ENV", "production");
    try {
      process.env.MEMORYLAB_PUBLIC_API_URL = "http://api.example.org";
      expect(() => webEnv()).toThrow(/refusing to start.*publicApiUrl/);
      process.env.MEMORYLAB_PUBLIC_API_URL = "https://api.example.org";
      process.env.MEMORYLAB_API_URL = "https://api.example.org";
      process.env.MEMORYLAB_AUTH_URL = "https://auth.example.org/auth/v1";
      process.env.MEMORYLAB_SITE_URL = "https://lab.example.org";
      expect(() => webEnv()).toThrow(/WEB_TRUSTED_HOPS/);                       // no trusted LB configured: refused
      process.env.MEMORYLAB_WEB_TRUSTED_HOPS = "1";
      expect(() => webEnv()).not.toThrow();
    } finally {
      delete process.env.MEMORYLAB_PUBLIC_API_URL;
      delete process.env.MEMORYLAB_WEB_TRUSTED_HOPS;
      vi.unstubAllEnvs();
    }
  });
});

describe("token helpers", () => {
  it("reads exp and decides on refresh with a margin", () => {
    expect(jwtExp(jwt(1234))).toBe(1234);
    expect(jwtExp("garbage")).toBe(0);
    const now = 10_000;
    expect(needsRefresh(jwt(now + 30), now)).toBe(true);
    expect(needsRefresh(jwt(now + 600), now)).toBe(false);
    expect(needsRefresh(undefined, now)).toBe(true);
    expect(needsRefresh("garbage", now)).toBe(true);
  });
  it("proxy DELETE and unknown verbs are handled by the same guard", async () => {
    const r = await proxyDelete(req("/bff/api/runs/x", { method: "DELETE", origin: "https://evil.example" }), ctx("runs/x"));
    expect(r.status).toBe(403);
  });
});

describe("limiter memory", () => {
  it("drops keys once their window has passed and caps the map", async () => {
    const { limited, limiterSize, sweepLimiter } = await import("@/server/guard");
    const t0 = 1_000_000;
    sweepLimiter(t0 + 10 ** 13);                               // start from an empty map whatever earlier tests left
    for (let i = 0; i < 50; i++) limited(`k${i}`, 5, 60, t0);
    expect(limiterSize()).toBe(50);
    sweepLimiter(t0 + 30_000);
    expect(limiterSize()).toBe(50);                            // still inside the window
    sweepLimiter(t0 + 61_000);
    expect(limiterSize()).toBe(0);                             // all gone
    for (let i = 0; i < 10_500; i++) limited(`flood${i}`, 5, 3600, t0);
    expect(limiterSize()).toBeLessThanOrEqual(10_000);         // capped, oldest evicted
    expect(limited("flood10499", 1, 3600, t0)).toBe(true);     // the newest key was kept (and is at its limit)
    sweepLimiter(t0 + 10 ** 13);
  });
  it("still limits correctly", async () => {
    const { limited } = await import("@/server/guard");
    const r = [1, 2, 3, 4].map(() => limited("same", 3, 60, 5_000_000));
    expect(r).toEqual([false, false, false, true]);
    expect(limited("same", 3, 60, 5_000_000 + 61_000)).toBe(false);
  });
});
