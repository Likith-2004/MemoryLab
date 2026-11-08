import { defineConfig, devices } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

/** The browser tests run against a PRODUCTION build (`next build` first: `make web-e2e` / the gate does it) and the real API stack
 *  (e2e/stack.py: real GoTrue, Postgres, accounts service, a scripted orchestrator). They need `supabase start`. */
const infra = join(import.meta.dirname, "..", "..", "infra");
const sb = Object.fromEntries(
  execFileSync(join(homedir(), ".local/bin/supabase"), ["status", "--workdir", infra, "-o", "env"], { encoding: "utf8" })
    .split("\n").filter((l) => l.includes("=")).map((l) => { const i = l.indexOf("="); return [l.slice(0, i), l.slice(i + 1).replace(/^"|"$/g, "")]; }),
) as Record<string, string>;

// One private directory per run holds the object store, the seed file and the web server log, so two runs never clobber each other.
// Workers inherit it through the environment.
const dir = (process.env.E2E_DIR ??= mkdtempSync(join(tmpdir(), "memorylab-e2e-")));
const WEB = "http://localhost:3010";
const API = "http://127.0.0.1:8010";
export const CANARY = "sk-ant-CANARY-0123456789abcdef";
const python = join(import.meta.dirname, "..", "..", ".venv", "bin", "python");

export default defineConfig({
  testDir: "e2e",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  use: { baseURL: WEB, trace: "retain-on-failure", reducedMotion: "no-preference" },
  projects: [{ name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } }],
  webServer: [
    {
      command: `${python} e2e/stack.py`,
      url: `${API}/api/v1/health`,
      timeout: 90_000,
      reuseExistingServer: false,
      gracefulShutdown: { signal: "SIGTERM", timeout: 60_000 },   // let the harness delete what it created
      env: { E2E_WEB_ORIGIN: WEB, E2E_CANARY_KEY: CANARY, E2E_API_PORT: "8010", E2E_DIR: dir },
    },
    {
      // stdout/stderr go to a file so the canary test can prove the web server never logged the provider key
      command: `sh -c 'npx next start -p 3010 > ${dir}/web.log 2>&1'`,
      url: `${WEB}/leaderboard`,
      timeout: 60_000,
      reuseExistingServer: false,
      env: {
        MEMORYLAB_API_URL: API, MEMORYLAB_PUBLIC_API_URL: API, MEMORYLAB_AUTH_URL: `${sb.API_URL}/auth/v1`,
        MEMORYLAB_PUBLISHABLE_KEY: sb.PUBLISHABLE_KEY ?? "", MEMORYLAB_SITE_URL: WEB, MEMORYLAB_WEB_TRUSTED_HOPS: "1",
        MEMORYLAB_CSP_REPORT_URI: "/csp-report", MEMORYLAB_LOGIN_LIMIT_PER_MIN: "500", NODE_ENV: "production",
      },
    },
  ],
});
