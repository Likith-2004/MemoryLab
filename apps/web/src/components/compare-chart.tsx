import type { Compare } from "@/lib/types";

const DASHES = ["", "6 3", "2 3", "8 3 2 3", "1 4", "10 4"];

/** Step functions, as the API returns them: a value carries forward until the run's next observation. No smoothing, no invented points.
 *  Colours come from CSS variables via classes; each run also has its own dash pattern. */
export function CompareChart({ data, labels }: { data: Compare; labels: Record<string, string> }) {
  const W = 640, H = 240, L = 44, R = 8, T = 8, B = 24;
  const xs = data.steps;
  const all = data.series.flatMap((s) => s.values).filter((v): v is number => v !== null);
  if (xs.length === 0 || all.length === 0) return <p className="border border-line bg-panel p-3 text-sm text-muted">No observations to chart yet.</p>;
  const x0 = xs[0]!, x1 = Math.max(xs.at(-1)!, x0 + 1);
  const lo = Math.min(0, ...all), hi = Math.max(1, ...all);
  const px = (s: number) => L + ((s - x0) / (x1 - x0)) * (W - L - R);
  const py = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${data.metric} by step for ${data.series.length} runs`} className="w-full border border-line bg-panel">
        <line x1={L} x2={L} y1={T} y2={H - B} className="stroke-line" />
        <line x1={L} x2={W - R} y1={H - B} y2={H - B} className="stroke-line" />
        {[lo, (lo + hi) / 2, hi].map((v) => <g key={v}><line x1={L} x2={W - R} y1={py(v)} y2={py(v)} className="stroke-line" strokeDasharray="1 4" /><text x={L - 4} y={py(v) + 4} textAnchor="end" className="mono fill-muted" fontSize="11">{Math.round(v * 10) / 10}</text></g>)}
        <text x={L} y={H - 6} className="mono fill-muted" fontSize="11">{x0}</text>
        <text x={W - R} y={H - 6} textAnchor="end" className="mono fill-muted" fontSize="11">step {x1}</text>
        {data.series.map((s, i) => {
          let d = "";
          let prev: number | null = null;
          xs.forEach((step, k) => {
            const v = s.values[k];
            if (v === null || v === undefined) return;
            d += prev === null ? `M${px(step)} ${py(v)}` : `L${px(step)} ${py(prev)}L${px(step)} ${py(v)}`;
            prev = v;
          });
          return <path key={s.run_id} d={d} fill="none" strokeWidth="2" strokeDasharray={DASHES[i % DASHES.length]} stroke={`var(--run-${(i % 6) + 1})`} />;
        })}
      </svg>
      <figcaption className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {data.series.map((s, i) => (
          <span key={s.run_id} className="flex items-center gap-1">
            <svg width="28" height="8" aria-hidden="true"><line x1="0" x2="28" y1="4" y2="4" strokeWidth="2" strokeDasharray={DASHES[i % DASHES.length]} stroke={`var(--run-${(i % 6) + 1})`} /></svg>
            <span className="mono">{labels[s.run_id] ?? s.run_id.slice(0, 8)}</span>
          </span>
        ))}
      </figcaption>
    </figure>
  );
}
