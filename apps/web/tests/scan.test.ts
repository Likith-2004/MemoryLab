import { describe, expect, it } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { scanDir, scanText } from "../scripts/scan-lib.mjs";

const jwt = (role: string) =>
  ["eyJhbGciOiJIUzI1NiJ9", Buffer.from(JSON.stringify({ role })).toString("base64url").replace(/^/, ""), "c2lnbmF0dXJlLXNpZ25hdHVyZQ"].join(".");

describe("bundle secret scan", () => {
  const rules = (text: string, forbidden: string[] = []): string[] => scanText(text, forbidden).map((f) => f.rule);

  it("flags provider keys, secret keys, key blocks and tokens", () => {
    expect(rules("x = 'sk-ant-api03-" + "A".repeat(30) + "'")).toContain("anthropic_key");
    expect(rules("sk-" + "b".repeat(40))).toContain("openai_style_key");
    expect(rules("sb_secret_" + "c".repeat(20))).toContain("supabase_secret_key");
    expect(rules("process.env.SUPABASE_SECRET_KEY")).toContain("secret_key_name");
    expect(rules("-----BEGIN PRIVATE KEY-----")).toContain("private_key_block");
    expect(rules("AKIA" + "ABCDEFGHIJKLMNOP")).toContain("aws_access_key");
    expect(rules("Bearer " + "t".repeat(40))).toContain("bearer_token");
  });
  it("flags the service-role word in any spelling", () => {
    for (const w of ["service_role", "SERVICE_ROLE", "service-role", "serviceRole"]) expect(rules(`const k = '${w}'`)).toContain("service_role_word");
  });
  it("judges a JWT by its role: anon and authenticated are public, anything else is a leak", () => {
    expect(rules(jwt("anon"))).toEqual([]);
    expect(rules(jwt("authenticated"))).toEqual([]);
    expect(rules(jwt("service_role"))).toEqual(expect.arrayContaining(["jwt_role_service_role"]));
    expect(rules(jwt("supabase_admin"))).toContain("jwt_role_supabase_admin");
  });
  it("accepts the publishable key and ordinary code", () => {
    expect(rules("const key = 'sb_publishable_" + "x".repeat(24) + "'; function f(){return 1}")).toEqual([]);
  });
  it("flags configured literal secret values", () => {
    expect(rules("...hunter2-hunter2...", ["hunter2-hunter2"])).toEqual(["forbidden_value"]);
  });
  it("never reports the matched text", () => {
    const secret = "sk-ant-api03-" + "Z".repeat(30);
    expect(JSON.stringify(scanText(secret))).not.toContain("ZZZZ");
  });
  it("walks a build directory, skipping build caches", () => {
    const dir = mkdtempSync(join(tmpdir(), "scan-"));
    mkdirSync(join(dir, "static"));
    mkdirSync(join(dir, "cache"));
    writeFileSync(join(dir, "static", "ok.js"), "console.log('hi')");
    writeFileSync(join(dir, "cache", "leak.js"), "sb_secret_" + "d".repeat(20));
    expect(scanDir(dir)).toEqual([]);
    writeFileSync(join(dir, "static", "bad.js"), "sk-ant-api03-" + "Q".repeat(30));
    expect(scanDir(dir).map((r) => r.rule)).toContain("anthropic_key");
  });
});
