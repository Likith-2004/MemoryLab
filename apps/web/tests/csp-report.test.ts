import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import { POST } from "@/app/csp-report/route";

afterEach(() => vi.restoreAllMocks());
const post = (body: string) => POST(new NextRequest("http://localhost:3000/csp-report", { method: "POST", body, headers: { "content-type": "application/csp-report" } }));

describe("CSP report endpoint", () => {
  it("logs the directive and the blocked origin only: never the URL, which can carry a token", async () => {
    const logs: string[] = [];
    vi.spyOn(console, "warn").mockImplementation((...a) => { logs.push(a.join(" ")); });
    const r = await post(JSON.stringify({ "csp-report": { "violated-directive": "img-src", "blocked-uri": "https://evil.example/pixel.png?token=SECRET123" } }));
    expect(r.status).toBe(204);
    expect(logs).toEqual(["csp-violation directive=img-src blocked=https://evil.example"]);
    expect(logs.join()).not.toContain("SECRET123");
  });
  it("ignores junk and oversized bodies without failing", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect((await post("not json")).status).toBe(204);
    expect((await post("x".repeat(9000))).status).toBe(204);
    expect(warn).not.toHaveBeenCalled();
  });
});
