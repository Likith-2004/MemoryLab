import { describe, expect, it } from "vitest";
import { derive, describeEvent, memoryVersions, percent, toRows } from "@/lib/runmodel";
import type { RunEvent } from "@/lib/types";

let seq = 0;
const ev = (type: string, step: number, data: Record<string, unknown> = {}, ts = `2026-11-09T00:00:${String(step % 60).padStart(2, "0")}.000Z`): RunEvent =>
  ({ seq: ++seq, run_id: "r", step, ts, type, data });

const log = (): RunEvent[] => {
  seq = 0;
  return [
    ev("run.state", 0, { state: "queued" }), ev("run.state", 0, { state: "running" }),
    ev("obs", 1, { score: 1, changed: true }), ev("model.call", 1, { tokens_total: 100 }),
    ev("mem.write", 2, { path: "notes/a.md", version: 1, chars: 5, created: true }),
    ev("obs", 2, { score: 3, changed: true }), ev("model.call", 2, { tokens_total: 250 }),
    ev("session.boundary", 3, {}),
    ev("mem.write", 4, { path: "notes/a.md", version: 2, chars: 9 }),
    ev("mem.delete", 5, { path: "notes/a.md", version: 3 }),
    ev("obs", 5, { score: 7 }),
    ev("run.state", 5, { state: "completed", reason: "step_cap" }),
  ];
};

describe("derive", () => {
  it("summarises the whole log", () => {
    const d = derive(log());
    expect(d).toMatchObject({ state: "completed", reason: "step_cap", step: 5, tokens: 250, score: 7, lastObsStep: 5, ended: true, lastSeq: 12 });
    expect(d.memoryTicks).toEqual([2, 4, 5]);
    expect(d.boundaries).toEqual([3]);
    expect(d.wallS).toBe(5);
  });
  it("shows the state at an earlier step (replay)", () => {
    const d = derive(log(), 2);
    expect(d).toMatchObject({ state: "running", step: 2, tokens: 250, score: 3, ended: false });
    expect(d.memoryTicks).toEqual([2]);
    expect(d.boundaries).toEqual([]);
    expect(derive(log(), 0).state).toBe("running");
  });
  it("is empty for an empty log", () => {
    expect(derive([])).toMatchObject({ state: null, step: 0, score: 0, lastObsStep: null, ended: false, wallS: null });
  });
  it("counts an injected memory read but not a plain read as a memory tick", () => {
    seq = 0;
    const d = derive([ev("mem.read", 1, { op: "inject", path: "g.md" }), ev("mem.read", 2, { path: "x" })]);
    expect(d.memoryTicks).toEqual([]);                                // ticks are changes, reads are shown in the log only
  });
});

describe("rows", () => {
  it("renders every event as plain text and flags memory events", () => {
    const rows = toRows(log(), false);
    expect(rows).toHaveLength(12);
    const w = rows.find((r) => r.type === "mem.write");
    expect(w).toMatchObject({ memory: true, text: "created notes/a.md v1 (5 chars)" });
    expect(rows.find((r) => r.type === "obs")?.memory).toBe(false);
  });
  it("groups consecutive identical no-effect rows with a count, and only those", () => {
    seq = 0;
    const events = [ev("action", 1, { ok: true, action: { a: 1 } }), ev("action", 2, { ok: true, action: { a: 1 } }), ev("action", 3, { ok: true, action: { a: 1 } }),
      ev("mem.write", 4, { path: "a", version: 1, chars: 1 }), ev("mem.write", 5, { path: "a", version: 1, chars: 1 })];
    const grouped = toRows(events, true);
    expect(grouped.map((r) => [r.type, r.count])).toEqual([["action", 3], ["mem.write", 1], ["mem.write", 1]]);
    expect(toRows(events, false)).toHaveLength(5);
  });
  it("respects the replay cursor and a row cap", () => {
    expect(toRows(log(), false, 2).every((r) => r.step <= 2)).toBe(true);
    expect(toRows(log(), false, undefined, 3)).toHaveLength(3);
  });
  it("never produces markup: hostile text stays a string", () => {
    seq = 0;
    const evil = "<img src=x onerror=alert(1)><script>alert(2)</script>";
    const rows = toRows([ev("model.summary", 1, { text: evil }), ev("error", 1, { code: "x", message: evil })], false);
    expect(rows[0]!.text).toBe(evil);                                 // returned verbatim; React renders it as a text node
    expect(typeof rows[0]!.text).toBe("string");
  });
  it("clips very long text", () => {
    seq = 0;
    expect(toRows([ev("model.summary", 1, { text: "x".repeat(5000) })], false)[0]!.text).toHaveLength(400);
  });
});

describe("memory versions and helpers", () => {
  it("lists versions from the log, honouring the cursor", () => {
    expect(memoryVersions(log()).map((v) => [v.version, v.deleted])).toEqual([[1, false], [2, false], [3, true]]);
    expect(memoryVersions(log(), 3)).toHaveLength(1);
  });
  it("describes every known event type without throwing on odd data", () => {
    for (const type of ["run.state", "obs", "action", "model.call", "model.summary", "mem.write", "mem.delete", "mem.read", "mem.reject",
      "session.boundary", "checkpoint", "cap.hit", "error", "dream.consolidate", "detector"]) {
      seq = 0;
      expect(typeof describeEvent(ev(type, 1, {}))).toBe("string");
      expect(typeof describeEvent(ev(type, 1, { path: 5, version: "x", text: null, action: [1] }))).toBe("string");
    }
  });
  it("percent is clamped", () => {
    expect([percent(50, 100), percent(500, 100), percent(1, 0), percent(0, 10)]).toEqual([50, 100, 0, 0]);
  });
});
