import type { RunEvent, RunStatus } from "./types";
import { FINAL_STATES } from "./types";

/** Everything the run screen shows is derived from the event log (the same log live and replay read), never from separate state. */

export interface Derived {
  state: RunStatus | null;
  reason: string | null;
  step: number; // highest step seen
  tokens: number;
  score: number;
  lastObsStep: number | null;
  memoryTicks: number[]; // steps at which memory changed (write, delete, reject), ascending, unique
  boundaries: number[]; // session boundaries
  lastSeq: number;
  ended: boolean;
  wallS: number | null; // seconds between the first and the last event
}

const MEMORY_EVENTS = new Set(["mem.write", "mem.delete", "mem.reject"]);

export function isMemoryEvent(e: RunEvent): boolean {
  return MEMORY_EVENTS.has(e.type) || e.type.startsWith("dream.") || (e.type === "mem.read" && e.data.op === "inject");
}

/** State as of `at` (a step) when given, else as of the end of the log. */
export function derive(events: readonly RunEvent[], at?: number): Derived {
  const d: Derived = { state: null, reason: null, step: 0, tokens: 0, score: 0, lastObsStep: null, memoryTicks: [], boundaries: [],
    lastSeq: 0, ended: false, wallS: null };
  const ticks = new Set<number>();
  let first: number | null = null;
  let last: number | null = null;
  for (const e of events) {
    if (at !== undefined && e.step > at) continue;
    d.lastSeq = Math.max(d.lastSeq, e.seq);
    d.step = Math.max(d.step, e.step);
    const t = Date.parse(e.ts);
    if (!Number.isNaN(t)) {
      first = first === null ? t : Math.min(first, t);
      last = last === null ? t : Math.max(last, t);
    }
    if (e.type === "run.state") {
      d.state = String(e.data.state) as RunStatus;
      d.reason = typeof e.data.reason === "string" ? e.data.reason : null;
      d.ended = FINAL_STATES.includes(d.state);
    } else if (e.type === "obs") {
      d.lastObsStep = e.step;
      if (typeof e.data.score === "number") d.score = e.data.score;
    } else if (e.type === "model.call" && typeof e.data.tokens_total === "number") {
      d.tokens = e.data.tokens_total;
    } else if (e.type === "session.boundary") {
      d.boundaries.push(e.step);
    }
    if (isMemoryEvent(e) && e.type !== "mem.read") ticks.add(e.step);
  }
  d.memoryTicks = [...ticks].sort((a, b) => a - b);
  d.wallS = first !== null && last !== null ? Math.round((last - first) / 1000) : null;
  return d;
}

export interface LogRow {
  key: string;
  seq: number;
  step: number;
  type: string;
  text: string;
  memory: boolean;
  count: number; // > 1 when repeats were grouped
}

/** One line per event: plain text only. Long text is clipped here; the full event stays in the log. */
export function describeEvent(e: RunEvent): string {
  const d = e.data;
  const s = (v: unknown): string => (typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v));
  switch (e.type) {
    case "run.state": return `${s(d.state)}${d.reason ? ` (${s(d.reason)})` : ""}`;
    case "obs": return `score ${s(d.score)}${d.new_milestones && (d.new_milestones as unknown[]).length ? ` · milestone ${s(d.new_milestones)}` : ""}${d.changed ? "" : " · no change"}`;
    case "action": return d.ok === false ? `action refused: ${s(d.error)}` : `action ${s(d.action)}`;
    case "model.call": return `model call · ${s(d.tokens_total)} tokens total`;
    case "model.summary": return s(d.text);
    case "mem.write": return `${d.created ? "created" : "wrote"} ${s(d.path)} v${s(d.version)} (${s(d.chars)} chars)`;
    case "mem.delete": return `deleted ${s(d.path)} v${s(d.version)}`;
    case "mem.read": return d.op === "inject" ? `injected ${s(d.path)}${d.found ? ` (${s(d.chars)} chars)` : " (missing)"}` : `read ${s(d.path)}`;
    case "mem.reject": return `memory write rejected: ${s(d.reason ?? d.code)}`;
    case "session.boundary": return `session boundary${d.session !== undefined ? ` ${s(d.session)}` : ""}`;
    case "checkpoint": return `checkpoint ${s(d.id ?? "")}`.trim();
    case "cap.hit": return `cap hit: ${s(d.cap)} (${s(d.value)} / ${s(d.limit)})`;
    case "error": return `error ${s(d.code)}: ${s(d.message)}`;
    default: return `${e.type} ${JSON.stringify(d)}`;
  }
}

export function toRows(events: readonly RunEvent[], group: boolean, at?: number, max = 2000): LogRow[] {
  const rows: LogRow[] = [];
  for (const e of events) {
    if (at !== undefined && e.step > at) continue;
    const text = describeEvent(e).slice(0, 400);
    const prev = rows[rows.length - 1];
    if (group && prev && prev.type === e.type && prev.text === text && (e.type === "action" || e.type === "obs")) {
      prev.count += 1;
      continue;
    }
    rows.push({ key: String(e.seq), seq: e.seq, step: e.step, type: e.type, text, memory: isMemoryEvent(e), count: 1 });
  }
  return rows.slice(-max);
}

export interface MemVersion { path: string; version: number; step: number; chars: number; deleted: boolean }

/** File versions as the log recorded them (path, version, step), newest last. */
export function memoryVersions(events: readonly RunEvent[], at?: number): MemVersion[] {
  const out: MemVersion[] = [];
  for (const e of events) {
    if (at !== undefined && e.step > at) continue;
    if ((e.type === "mem.write" || e.type === "mem.delete") && typeof e.data.path === "string" && typeof e.data.version === "number") {
      out.push({ path: e.data.path, version: e.data.version, step: e.step, chars: Number(e.data.chars ?? 0), deleted: e.type === "mem.delete" });
    }
  }
  return out;
}

export function percent(value: number, cap: number): number {
  return cap > 0 ? Math.min(100, Math.round((value / cap) * 100)) : 0;
}
