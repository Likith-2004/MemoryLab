"use client";
/** A scrubbable step axis. Memory changes are ticks in the memory colour, session boundaries are dashed; ticks are SVG attributes (no
 *  inline styles, which our CSP forbids). The range input is the keyboard control: arrows step, `[` and `]` jump between memory ticks. */
export function Timeline({ max, value, onChange, memoryTicks, boundaries, disabled }: {
  max: number; value: number; onChange: (step: number) => void; memoryTicks: number[]; boundaries: number[]; disabled?: boolean;
}) {
  const span = Math.max(max, 1);
  const x = (step: number) => `${(step / span) * 100}%`;
  return (
    <div className="mt-2">
      <svg role="img" aria-label={`${memoryTicks.length} memory changes along ${max} steps`} className="block h-5 w-full" preserveAspectRatio="none">
        <line x1="0" x2="100%" y1="10" y2="10" className="stroke-line" strokeWidth="1" />
        {boundaries.map((s) => <line key={`b${s}`} x1={x(s)} x2={x(s)} y1="2" y2="18" className="stroke-muted" strokeWidth="1" strokeDasharray="2 2" />)}
        {memoryTicks.map((s) => <line key={`m${s}`} x1={x(s)} x2={x(s)} y1="3" y2="17" className="stroke-memory" strokeWidth="2" />)}
      </svg>
      <input type="range" min={0} max={Math.max(max, 0)} step={1} value={Math.min(value, Math.max(max, 0))} disabled={disabled || max <= 0}
        aria-label="Step" aria-valuetext={`step ${value} of ${max}`} className="w-full"
        onChange={(e) => onChange(Number(e.target.value))}
        onKeyDown={(e) => {
          const jump = e.key === "]" ? memoryTicks.find((s) => s > value) : e.key === "[" ? [...memoryTicks].reverse().find((s) => s < value) : undefined;
          if (jump !== undefined) { e.preventDefault(); onChange(jump); }
        }} />
      <div className="mono flex justify-between text-xs text-muted"><span>0</span><span>step {value}</span><span>{max}</span></div>
    </div>
  );
}
