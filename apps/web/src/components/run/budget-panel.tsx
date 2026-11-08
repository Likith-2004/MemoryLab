import type { Run } from "@/lib/types";
import { percent } from "@/lib/runmodel";

function Meter({ label, value, cap, unit = "" }: { label: string; value: number; cap: number; unit?: string }) {
  const pct = percent(value, cap);
  return (
    <div className="mb-3">
      <div className="mb-1 flex justify-between text-sm"><span>{label}</span><span className="mono">{value.toLocaleString()}{unit} / {cap.toLocaleString()}{unit}</span></div>
      <progress className="h-2 w-full" max={100} value={pct} aria-label={`${label}: ${pct}% of the cap`} />
    </div>
  );
}

export function BudgetPanel({ run, step, tokens, wallS }: { run: Run; step: number; tokens: number; wallS: number | null }) {
  return (
    <section aria-labelledby="bud-h" className="border border-line bg-panel p-2">
      <h2 id="bud-h" className="mb-2 text-sm font-semibold">Budget</h2>
      <Meter label="Steps" value={step} cap={run.caps.max_steps} />
      <Meter label="Tokens" value={tokens} cap={run.caps.max_tokens} />
      <Meter label="Wall clock" value={wallS ?? 0} cap={run.caps.max_wall_s} unit=" s" />
      <p className="text-xs text-muted">No cost estimate is shown: no price table is loaded.</p>
    </section>
  );
}
