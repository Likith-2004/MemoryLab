import type { LeaderRow } from "./types";

export function median(values: number[]): number | null {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2;
}

export interface Group { model: string; n: number; median: number; min: number; max: number }

/** Per model: median, range and n over the verified runs. Never a bare mean, never without n. */
export function groupByModel(rows: LeaderRow[]): Group[] {
  const by = new Map<string, number[]>();
  for (const r of rows) by.set(r.model_id ?? "unknown model", [...(by.get(r.model_id ?? "unknown model") ?? []), r.progress_score]);
  return [...by].map(([model, v]) => ({ model, n: v.length, median: median(v)!, min: Math.min(...v), max: Math.max(...v) }))
    .sort((a, b) => b.median - a.median || b.n - a.n);
}
