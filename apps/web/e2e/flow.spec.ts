import { lstatSync, readdirSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CANARY, bff, expect, seed, signIn, test, waitIdle } from "./fixtures";

function filesUnder(dir: string, skip: (p: string) => boolean, depth = 6): string[] {
  const out: string[] = [];
  let names: string[] = [];
  try { names = readdirSync(dir); } catch { return out; }
  for (const name of names) {
    const p = join(dir, name);
    if (skip(p)) continue;
    let st;
    try { st = lstatSync(p); } catch { continue; } // lstat: never follow a symlink (temp directories can loop)
    if (st.isDirectory() && depth > 0) out.push(...filesUnder(p, skip, depth - 1));
    else if (st.isFile() && st.size < 20_000_000) out.push(p);
  }
  return out;
}

test("launch, watch live, open the memory drawer, scrub the replay, export", async ({ page }) => {
  const startedAt = Date.now();
  const s = seed();
  await signIn(page, s.owner, s.password, "/runs/new");
  await waitIdle(page);
  await expect(page.getByRole("heading", { name: "New run" })).toBeVisible();

  // step 1 mode, 2 game, 3 model and preset (the full preset text is visible), 4 caps, then review
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Game").selectOption(s.game);
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByLabel("Preset text")).not.toBeEmpty();
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Review" }).click();
  await expect(page.getByText("Config hash")).toBeVisible();

  const key = page.getByLabel("Your provider API key");
  await expect(key).toHaveAttribute("type", "password");
  await expect(key).toHaveAttribute("autocomplete", "off");
  await key.fill(CANARY);
  await page.getByRole("button", { name: "Start run" }).click();

  // live: the run page, the first frame, memory appearing, a session boundary, then the end
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/);
  const url = page.url();
  await expect(page.getByRole("region", { name: "Run status" })).toContainText(/running|queued|starting|completed/);
  await expect(page.getByLabel("Run events")).toContainText("model call", { timeout: 20_000 });
  await expect(page.getByRole("img", { name: /Game screen at step/ })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("heading", { name: /Memory at step/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /notes\/a\.md/ })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("status").filter({ hasText: /^Run completed/ })).toBeVisible({ timeout: 30_000 });

  // the key is gone from the form and was never stored in the page
  expect(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage, document.cookie]))).not.toContain(CANARY);
  expect(await page.content()).not.toContain(CANARY);

  // memory drawer: versions, plain-text content, diff
  await page.getByRole("button", { name: /notes\/a\.md/ }).click();
  const drawer = page.getByRole("dialog", { name: "notes/a.md" });
  await expect(drawer.locator("pre")).toContainText("notes v");
  await drawer.getByRole("button", { name: "diff" }).click();
  await expect(drawer.locator("pre")).toContainText("---");
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();

  // replay: scrub back; the status strip, the log and the memory tree follow the cursor
  const slider = page.getByRole("slider", { name: "Step" });
  await slider.fill("1");
  await expect(page.getByText("step 1 / ")).toBeVisible();
  await expect(page.getByText("The agent has not written any memory yet.")).toBeVisible();
  await slider.fill("8");
  await expect(page.getByRole("button", { name: /notes\/a\.md/ })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("at")).toBe("8");
  await page.goto(`${url}?at=3`);
  await expect(page.getByRole("slider", { name: "Step" })).toHaveValue("3");

  // export: a verified bundle downloads and the sha is shown
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Export" }).click()]);
  expect(download.suggestedFilename()).toMatch(/\.zip$/);
  await expect(page.getByText(/SHA-256: [0-9a-f]{64}/)).toBeVisible();
  const runId = url.split("/").pop()!;
  const exp = await bff<{ sha256: string }>(page, `/runs/${runId}/export`);
  expect(exp.status).toBe(200);

  // canary: the provider key is in no log, no cache and no temp file the web server or this test run wrote
  const roots = [join(import.meta.dirname, "..", ".next", "cache"), "/tmp/memorylab-e2e-web.log"];
  const hits: string[] = [];
  for (const root of roots) {
    let paths: string[] = [];
    try { paths = statSync(root).isDirectory() ? filesUnder(root, () => false) : [root]; } catch { continue; }
    for (const f of paths) if (readFileSync(f).includes(CANARY)) hits.push(f);
  }
  // anything under the temp directory written since this test began (the object store is included on purpose: events, frames, bundles)
  const noise = /playwright|chromium|\.X11|systemd|snap|\.org\.chromium|ms-playwright|pytest-of|npm-|_MEI|mlrev/;
  for (const f of filesUnder(tmpdir(), (p) => noise.test(p)).slice(0, 20000)) {
    try { if (statSync(f).mtimeMs >= startedAt - 1000 && readFileSync(f).includes(CANARY)) hits.push(f); } catch { /* unreadable */ }
  }
  expect(hits, `provider key found in: ${hits.join(", ")}`).toEqual([]);
});

test("negative control: the canary scan does find a key that was written to a temp file", async () => {
  const { writeFileSync, unlinkSync } = await import("node:fs");
  const f = join(tmpdir(), `memorylab-e2e-negative-control-${Date.now()}.txt`);
  writeFileSync(f, `leak ${CANARY}`);
  try {
    const found = filesUnder(tmpdir(), (p) => /playwright|chromium|mlrev/.test(p)).filter((p) => { try { return readFileSync(p).includes(CANARY); } catch { return false; } });
    expect(found).toContain(f);
  } finally { unlinkSync(f); }
});
