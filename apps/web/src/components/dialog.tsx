"use client";
import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";

/* A modal dialog on the native <dialog> element: focus is trapped and Escape closes it by the platform, with no library.
 * We evaluated Radix Dialog (and shadcn/ui's wrapper): it injects inline style attributes and an un-nonced <style> element to lock
 * scrolling, which our production CSP (nonce-only styles, no 'unsafe-inline') blocks. Same API shape as the shadcn wrapper so callers
 * read the same. Recorded in docs/THIRD_PARTY.md (no code copied). */
const Ctx = createContext<{ onOpenChange: (open: boolean) => void }>({ onOpenChange: () => undefined });

export function Dialog({ open, onOpenChange, children }: { open: boolean; onOpenChange: (open: boolean) => void; children: ReactNode }) {
  return <Ctx.Provider value={{ onOpenChange }}>{open ? children : null}</Ctx.Provider>;
}

export function DialogContent({ title, description, children, side = "center" }: { title: string; description?: string; children: ReactNode; side?: "center" | "right" }) {
  const { onOpenChange } = useContext(Ctx);
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && !el.open) el.showModal();
    return () => { if (el?.open) el.close(); };
  }, []);
  const place = side === "right"
    ? "ml-auto mr-0 mt-0 mb-0 h-full max-h-none w-full max-w-xl"
    : "m-auto max-h-[90vh] w-[calc(100%-2rem)] max-w-lg";
  return (
    <dialog ref={ref} aria-label={title} onClose={() => onOpenChange(false)}
      onClick={(e) => { if (e.target === ref.current) onOpenChange(false); }}
      className={`${place} overflow-y-auto border border-line bg-panel p-4 text-ink backdrop:bg-black/50`}>
      <div className="mb-3 flex items-start justify-between gap-4">
        <h2 className="text-lg font-semibold">{title}</h2>
        <button type="button" onClick={() => onOpenChange(false)} className="rounded border border-line px-2 py-0.5 text-sm">Close</button>
      </div>
      {description ? <p className="mb-3 text-sm text-muted">{description}</p> : null}
      {children}
    </dialog>
  );
}
