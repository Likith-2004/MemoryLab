import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { POST, logSafe } from "@/app/csp-report/route";

afterEach(() => vi.restoreAllMocks());
let n = 0;
const post = (body: BodyInit, headers: Record<string, string> = {}) =>
  POST(new NextRequest("http://localhost:3000/csp-report", { method: "POST", body, headers: { "content-type": "application/csp-report", "x-forwarded-for": `198.51.100.${++n % 250}`, ...headers } }));
const capture = () => { const logs: string[] = []; vi.spyOn(console, "warn").mockImplementation((...a) => { logs.push(a.join(" ")); }); vi.spyOn(console, "error").mockImplementation((...a) => { logs.push(a.join(" ")); }); return logs; };

describe("CSP report endpoint", () => {
  it("logs the directive and the blocked origin only: never the URL, which can carry a token", async () => {
    const logs = capture();
    const r = await post(JSON.stringify({ "csp-report": { "violated-directive": "img-src", "blocked-uri": "https://evil.example/pixel.png?token=SECRET123" } }));
    expect(r.status).toBe(204);
    expect(logs).toEqual(["csp-violation directive=img-src blocked=https://evil.example"]);
  });
  it("a newline in a field cannot forge a log line", async () => {
    const logs = capture();
    await post(JSON.stringify({ "csp-report": { "violated-directive": "script-src\nweb error id=FORGED where=login type=Admin", "blocked-uri": "x\r\nweb error id=FORGED2" } }));
    expect(logs).toHaveLength(1);
    expect(logs[0]).not.toMatch(/[\r\n]/);
    expect(logs[0]).not.toContain("web error");
    expect(logs[0]).toBe("csp-violation directive=script-src blocked=other");
  });
  it("logSafe keeps only plain characters and clips", () => {
    expect(logSafe("a\nb\u0000c<d>")).toBe("a?b?c?d?");
    expect(logSafe("x".repeat(500))).toHaveLength(60);
    expect(logSafe(undefined)).toBe("");
  });
  it("refuses a declared body over 8 KB without reading it", async () => {
    const r = await post("{}", { "content-length": "1000000" });
    expect(r.status).toBe(413);
  });
  it("refuses a 1 MB chunked body without buffering it all", async () => {
    let pulled = 0;
    const stream = new ReadableStream<Uint8Array>({ pull(c) { pulled += 64 * 1024; if (pulled > 4 * 1024 * 1024) c.close(); else c.enqueue(new Uint8Array(64 * 1024).fill(120)); } });
    const req = new NextRequest("http://localhost:3000/csp-report", { method: "POST", body: stream, headers: { "x-forwarded-for": "198.51.100.250" }, duplex: "half" } as unknown as ConstructorParameters<typeof NextRequest>[1]);
    const r = await POST(req);
    expect(r.status).toBe(413);
    expect(pulled).toBeLessThan(1024 * 1024);                      // it stopped reading long before the end
  });
  it("junk is ignored with a 204 and nothing logged", async () => {
    const logs = capture();
    expect((await post("not json")).status).toBe(204);
    expect(logs).toEqual([]);
  });
  it("is rate limited to 60 a minute per client address", async () => {
    process.env.MEMORYLAB_WEB_TRUSTED_HOPS = "1";
    try {
      const codes: number[] = [];
      for (let i = 0; i < 62; i++) codes.push((await post("{}", { "x-forwarded-for": "203.0.113.77" })).status);
      expect(codes.slice(0, 60).every((c) => c === 204)).toBe(true);
      expect(codes.slice(60)).toEqual([429, 429]);
      expect((await post("{}", { "x-forwarded-for": "203.0.113.78" })).status).toBe(204);   // another address is unaffected
    } finally { delete process.env.MEMORYLAB_WEB_TRUSTED_HOPS; }
  });
});
