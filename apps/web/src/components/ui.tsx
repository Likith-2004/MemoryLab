import type { ReactNode } from "react";

export function Button({ children, variant = "solid", ...rest }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "solid" | "plain" | "danger" }) {
  const base = "inline-flex items-center justify-center rounded border px-3 py-1.5 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed";
  const look = variant === "solid" ? "border-ink bg-ink text-surface" : variant === "danger" ? "border-bad text-bad bg-panel" : "border-line bg-panel text-ink";
  return <button type="button" {...rest} className={`${base} ${look} ${rest.className ?? ""}`}>{children}</button>;
}

export function Field({ label, hint, error, children, id }: { label: string; hint?: string; error?: string | null; children: ReactNode; id: string }) {
  return (
    <div className="mb-3">
      <label htmlFor={id} className="mb-1 block text-sm font-medium">{label}</label>
      {children}
      {hint ? <p id={`${id}-hint`} className="mt-1 text-xs text-muted">{hint}</p> : null}
      {error ? <p id={`${id}-err`} role="alert" className="mt-1 text-xs text-bad">{error}</p> : null}
    </div>
  );
}

export const inputClass = "w-full rounded border border-line bg-panel px-2 py-1.5 text-sm";

export function Banner({ tone, children, role }: { tone: "info" | "warn" | "bad" | "ok"; children: ReactNode; role?: "alert" | "status" }) {
  const look = { info: "border-line", warn: "border-warn text-warn", bad: "border-bad text-bad", ok: "border-ok text-ok" }[tone];
  return <div role={role ?? (tone === "bad" ? "alert" : "status")} className={`mb-3 rounded border bg-panel px-3 py-2 text-sm ${look}`}>{children}</div>;
}

export function PageTitle({ children, sub }: { children: ReactNode; sub?: ReactNode }) {
  return (
    <header className="mb-4">
      <h1 className="text-xl font-semibold">{children}</h1>
      {sub ? <p className="mt-1 text-sm text-muted">{sub}</p> : null}
    </header>
  );
}

/** Text only. Server-supplied strings (titles, model text, memory) go through here or a plain {text} child: never as HTML. */
export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`mono ${className}`}>{children}</span>;
}

export function StatusChip({ status }: { status: string }) {
  const tone = status === "completed" || status === "capped" ? "text-ok" : status === "failed" || status === "killed" ? "text-bad" : "text-warn";
  return <span className={`mono rounded border border-line px-1.5 py-0.5 text-xs ${tone}`}>{status.replace("_", " ")}</span>;
}
