/** Looks for anything that must never ship to a browser: service-role or secret keys, provider keys, private keys. */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

export const PATTERNS = {
  anthropic_key: /sk-ant-[A-Za-z0-9_-]{16,}/,
  openai_style_key: /sk-(?:proj-)?[A-Za-z0-9_-]{32,}/,
  supabase_secret_key: /sb_secret_[A-Za-z0-9_-]{8,}/,
  service_role_word: /service[_-]?role/i,
  secret_key_name: /SUPABASE_SECRET_KEY|SECRET_KEY|SERVICE_KEY/,
  aws_access_key: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/,
  google_api_key: /\bAIza[0-9A-Za-z_-]{35}\b/,
  private_key_block: /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  bearer_token: /Bearer\s+[A-Za-z0-9._~+/-]{30,}/,
};

const JWT = /eyJ[A-Za-z0-9_-]{8,}\.(eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9_-]{8,}/g;

/** Findings in one text: [{rule}] (never the matched text itself). A JWT is a finding unless its role is anon/authenticated. */
export function scanText(text, forbidden = []) {
  const found = [];
  for (const [rule, re] of Object.entries(PATTERNS)) if (re.test(text)) found.push({ rule });
  for (const m of text.matchAll(JWT)) {
    try {
      const role = JSON.parse(Buffer.from(m[1].replace(/-/g, "+").replace(/_/g, "/"), "base64").toString()).role;
      if (role !== "anon" && role !== "authenticated") found.push({ rule: `jwt_role_${String(role)}` });
    } catch {
      found.push({ rule: "jwt_unparseable" });
    }
  }
  for (const value of forbidden) if (value && text.includes(value)) found.push({ rule: "forbidden_value" });
  return found;
}

export function* walk(dir, skip = new Set()) {
  for (const name of readdirSync(dir)) {
    if (skip.has(name)) continue;
    const path = join(dir, name);
    const st = statSync(path);
    if (st.isDirectory()) yield* walk(path, skip);
    else if (st.isFile() && st.size < 20 * 1024 * 1024) yield path;
  }
}

/** Scans a build directory; `cache` and `dev` are build scratch, not shipped. */
export function scanDir(dir, forbidden = []) {
  const results = [];
  for (const file of walk(dir, new Set(["cache", "dev", "node_modules"]))) {
    if (/\.(png|jpg|jpeg|gif|ico|woff2?|map)$/.test(file)) continue;
    const text = readFileSync(file, "latin1");
    for (const f of scanText(text, forbidden)) results.push({ file, ...f });
  }
  return results;
}
