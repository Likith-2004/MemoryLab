"use client";
import type { LogRow } from "@/lib/runmodel";

export function LogPanel({ rows, fresh, group, onGroup }: { rows: LogRow[]; fresh: number | null; group: boolean; onGroup: (v: boolean) => void }) {
  return (
    <section aria-labelledby="log-h" className="border border-line bg-panel">
      <div className="flex items-center justify-between border-b border-line px-2 py-1">
        <h2 id="log-h" className="text-sm font-semibold">Log</h2>
        <label className="flex items-center gap-1 text-xs">
          <input type="checkbox" checked={group} onChange={(e) => onGroup(e.target.checked)} />
          Group repeats
        </label>
      </div>
      <ol className="max-h-[28rem] overflow-y-auto" tabIndex={0} aria-label="Run events">
        {rows.length === 0 ? <li className="p-3 text-sm text-muted">No events yet.</li> : null}
        {rows.map((r) => (
          <li key={r.key} className={`flex gap-2 border-b border-line px-2 py-1 ${r.seq === fresh ? "row-new" : ""}`}>
            <span className="mono w-14 shrink-0 text-right text-muted">{r.step}</span>
            <span className={`mono w-24 shrink-0 text-xs ${r.memory ? "font-semibold text-memory" : "text-muted"}`}>{r.type}</span>
            {/* text node only: model, memory and error text is attacker-influenced */}
            <span className="mono min-w-0 flex-1 break-words whitespace-pre-wrap">{r.text}{r.count > 1 ? ` ×${r.count}` : ""}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
