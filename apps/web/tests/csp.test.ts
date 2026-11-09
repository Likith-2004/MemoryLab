import { describe, expect, it } from "vitest";
import { buildCsp, newNonce } from "@/lib/csp";
import { wsOrigin } from "@/lib/env";

const base = { nonce: "abc123", apiOrigin: "https://api.example.org", wsOrigin: "wss://api.example.org" };

describe("content security policy", () => {
  const prod = buildCsp({ ...base, dev: false });
  const directive = (csp: string, name: string) => csp.split("; ").find((d) => d.startsWith(name + " ")) ?? "";

  it("production has no unsafe-inline and no unsafe-eval anywhere", () => {
    expect(prod).not.toContain("unsafe-inline");
    expect(prod).not.toContain("unsafe-eval");
    expect(prod).not.toContain("*");
  });
  it("scripts and styles need this request's nonce", () => {
    expect(directive(prod, "script-src")).toBe("script-src 'self' 'nonce-abc123' 'strict-dynamic'");
    expect(directive(prod, "style-src")).toBe("style-src 'self' 'nonce-abc123'");
  });
  it("forbids framing, plugins, base tag and form posts elsewhere", () => {
    for (const d of ["frame-ancestors 'none'", "object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-src 'none'",
      "upgrade-insecure-requests"]) expect(prod.split("; ")).toContain(d);
  });
  it("lets the page talk only to itself and the API's socket; images may come from the API (signed frame URLs)", () => {
    expect(directive(prod, "connect-src")).toBe("connect-src 'self' wss://api.example.org");
    expect(directive(prod, "img-src")).toBe("img-src 'self' data: https://api.example.org");
  });
  it("upgrade-insecure-requests follows the site's own scheme", () => {
    expect(buildCsp({ ...base, dev: false, upgrade: false })).not.toContain("upgrade-insecure-requests");
    expect(buildCsp({ ...base, dev: false, upgrade: true })).toContain("upgrade-insecure-requests");
  });
  it("only development adds unsafe-eval", () => {
    const dev = buildCsp({ ...base, dev: true });
    expect(directive(dev, "script-src")).toContain("'unsafe-eval'");
    expect(dev).not.toContain("unsafe-inline");
  });
  it("nonces are unique and unguessable-looking", () => {
    const seen = new Set(Array.from({ length: 200 }, () => newNonce()));
    expect(seen.size).toBe(200);
    for (const n of seen) expect(atob(n).length).toBe(16);
  });
  it("report-uri is added only when configured", () => {
    expect(prod).not.toContain("report-uri");
    expect(buildCsp({ ...base, dev: false, reportUri: "/csp-report" })).toContain("report-uri /csp-report");
  });
  it("derives the socket origin from the API url", () => {
    expect(wsOrigin("https://api.example.org")).toBe("wss://api.example.org");
    expect(wsOrigin("http://127.0.0.1:8000")).toBe("ws://127.0.0.1:8000");
  });
});
