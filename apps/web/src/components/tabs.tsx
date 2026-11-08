"use client";
import { useId, useRef, type ReactNode } from "react";

/** Tabs for phones (the panels are all visible side by side from md up). Written here instead of Radix Tabs because Radix renders an inline
 *  `style` attribute on every panel, which our production CSP forbids. ARIA tab pattern: roving tabindex, arrows/Home/End move focus. */
export interface TabDef { id: string; label: string; panel: ReactNode }

export function Tabs({ tabs, value, onChange }: { tabs: TabDef[]; value: string; onChange: (id: string) => void }) {
  const base = useId();
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const move = (to: number) => {
    const t = tabs[(to + tabs.length) % tabs.length]!;
    onChange(t.id);
    refs.current[t.id]?.focus();
  };
  return (
    <div className="min-w-0">
      <div role="tablist" aria-label="Run panels" className="mb-2 flex gap-1 md:hidden"
        onKeyDown={(e) => {
          const i = tabs.findIndex((t) => t.id === value);
          if (e.key === "ArrowRight") { e.preventDefault(); move(i + 1); }
          else if (e.key === "ArrowLeft") { e.preventDefault(); move(i - 1); }
          else if (e.key === "Home") { e.preventDefault(); move(0); }
          else if (e.key === "End") { e.preventDefault(); move(tabs.length - 1); }
        }}>
        {tabs.map((t) => (
          <button key={t.id} ref={(el) => { refs.current[t.id] = el; }} type="button" role="tab" id={`${base}-t-${t.id}`} aria-selected={t.id === value}
            aria-controls={`${base}-p-${t.id}`} tabIndex={t.id === value ? 0 : -1} onClick={() => onChange(t.id)}
            className={`rounded border px-2 py-1 text-sm ${t.id === value ? "border-ink bg-ink text-surface" : "border-line"}`}>{t.label}</button>
        ))}
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {tabs.map((t) => (
          // every panel stays mounted (state and scroll are kept); on phones only the selected one is shown, from md up all are
          <div key={t.id} role="tabpanel" id={`${base}-p-${t.id}`} aria-labelledby={`${base}-t-${t.id}`} className={`min-w-0 ${t.id === value ? "" : "max-md:hidden"}`}>{t.panel}</div>
        ))}
      </div>
    </div>
  );
}
