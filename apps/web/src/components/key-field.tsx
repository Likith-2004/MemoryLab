"use client";
import { Field, inputClass } from "./ui";

/** The provider key: a password-type field, autocomplete off, held only in the parent's state until submit, where the parent clears it.
 *  It is never written to storage, the URL or the log, and it is sent once, in the body of POST /runs (or /fork). */
export function KeyField({ value, onChange, error }: { value: string; onChange: (v: string) => void; error?: string | null }) {
  return (
    <>
      <Field id="provider-key" label="Your provider API key" error={error}
        hint="Used for this run only. It is sent to the lab once, held in memory while the run is queued and running, and never stored, logged or shown again.">
        <input id="provider-key" name="provider-key" type="password" autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false}
          data-1p-ignore data-lpignore="true" required minLength={8} maxLength={512} className={`${inputClass} mono`}
          value={value} onChange={(e) => onChange(e.target.value)} aria-describedby="provider-key-hint provider-key-err" />
      </Field>
      <p className="mb-3 text-xs text-muted">Use a key with a spend cap. The run stops at the caps you set below.</p>
    </>
  );
}

export interface Caps { max_steps: number; max_tokens: number; max_wall_s: number }

export function CapsFields({ caps, onChange }: { caps: Caps; onChange: (c: Caps) => void }) {
  const num = (name: keyof Caps, label: string, max: number) => (
    <Field id={`cap-${name}`} label={label} hint={`1 to ${max.toLocaleString()}. Required.`}>
      <input id={`cap-${name}`} type="number" inputMode="numeric" min={1} max={max} step={1} required className={`${inputClass} mono`}
        value={Number.isFinite(caps[name]) ? caps[name] : ""} onChange={(e) => onChange({ ...caps, [name]: e.target.value === "" ? NaN : Math.trunc(Number(e.target.value)) })} />
    </Field>
  );
  return (
    <fieldset className="mb-2 border border-line p-2">
      <legend className="px-1 text-sm font-medium">Caps</legend>
      {num("max_steps", "Max steps", 100_000)}
      {num("max_tokens", "Max tokens", 1_000_000_000)}
      {num("max_wall_s", "Max wall-clock seconds", 86_400)}
    </fieldset>
  );
}

export function capsValid(c: Caps): boolean {
  return Number.isInteger(c.max_steps) && c.max_steps >= 1 && c.max_steps <= 100_000
    && Number.isInteger(c.max_tokens) && c.max_tokens >= 1 && c.max_tokens <= 1_000_000_000
    && Number.isInteger(c.max_wall_s) && c.max_wall_s >= 1 && c.max_wall_s <= 86_400;
}
