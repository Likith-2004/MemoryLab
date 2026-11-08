"use client";
import { useEffect, useMemo, useState } from "react";
import { ApiProblem, api, apiText, describe } from "@/lib/client";
import type { MemoryNode, RunEvent } from "@/lib/types";
import { memoryVersions } from "@/lib/runmodel";
import { Dialog, DialogContent } from "../dialog";

/** The memory tree at the cursor step; a file opens in a drawer with its versions, plain-text content and diffs. */
export function MemoryPanel({ runId, atStep, events }: { runId: string; atStep: number; events: RunEvent[] }) {
  const [tree, setTree] = useState<MemoryNode[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const changes = events.filter((e) => e.type === "mem.write" || e.type === "mem.delete").length;
  const rejected = events.filter((e) => e.type === "mem.reject" && e.step <= atStep);

  useEffect(() => {
    let live = true;
    api<MemoryNode[]>(`/runs/${runId}/memory?at_step=${atStep}`).then((t) => { if (live) { setTree(t); setError(null); } })
      .catch((e: unknown) => { if (live) setError(describe(e)); });
    return () => { live = false; };
  }, [runId, atStep, changes]);

  return (
    <section aria-labelledby="mem-h" className="border border-line bg-panel">
      <h2 id="mem-h" className="border-b border-line px-2 py-1 text-sm font-semibold">Memory at step <span className="mono">{atStep}</span></h2>
      {error ? <p role="alert" className="p-2 text-sm text-bad">{error}</p> : null}
      {tree && tree.length === 0 ? <p className="p-3 text-sm text-muted">The agent has not written any memory yet.</p> : null}
      <ul>
        {(tree ?? []).map((n) => (
          <li key={n.path} className="border-b border-line">
            <button type="button" onClick={() => setOpen(n.path)} className="flex w-full items-baseline justify-between gap-2 px-2 py-1 text-left hover:bg-memory-soft">
              <span className="mono font-semibold text-memory break-all">{n.path}</span>
              <span className="mono shrink-0 text-xs text-muted">v{n.version} · {n.chars} chars</span>
            </button>
          </li>
        ))}
      </ul>
      {rejected.length ? (
        <details className="border-t border-line p-2 text-sm">
          <summary>{rejected.length} rejected write{rejected.length > 1 ? "s" : ""}</summary>
          <ul className="mt-1">{rejected.map((e) => <li key={e.seq} className="mono text-xs">step {e.step}: {String(e.data.reason ?? e.data.code ?? "rejected")}</li>)}</ul>
        </details>
      ) : null}
      <Dialog open={open !== null} onOpenChange={(o) => { if (!o) setOpen(null); }}>
        {open !== null ? <MemoryFile runId={runId} path={open} atStep={atStep} events={events} /> : null}
      </Dialog>
    </section>
  );
}

function MemoryFile({ runId, path, atStep, events }: { runId: string; path: string; atStep: number; events: RunEvent[] }) {
  const versions = useMemo(() => memoryVersions(events).filter((v) => v.path === path), [events, path]);
  const visible = versions.filter((v) => v.step <= atStep);
  const [version, setVersion] = useState<number | null>(visible.at(-1)?.version ?? versions.at(-1)?.version ?? null);
  const [view, setView] = useState<"content" | "diff">("content");
  const [body, setBody] = useState<{ text: string | null; error: string | null }>({ text: null, error: null });
  const current = versions.find((v) => v.version === version) ?? null;
  const previous = versions.filter((v) => v.version < (version ?? 0)).at(-1);

  useEffect(() => {
    if (version === null) return;
    let live = true;
    const q = new URLSearchParams({ path });
    const load = view === "diff" && previous
      ? apiText(`/runs/${runId}/memory/diff?${new URLSearchParams({ path, from: String(previous.version), to: String(version) })}`)
      : apiText(`/runs/${runId}/memory/file?${(q.set("version", String(version)), q)}`);
    load.then((text) => { if (live) setBody({ text, error: null }); })
      .catch((e: unknown) => { if (live) setBody({ text: null, error: e instanceof ApiProblem && e.status === 404 ? "This version has no content (the file was deleted)." : "Could not load the file." }); });
    return () => { live = false; };
  }, [runId, path, version, view, previous]);

  return (
    <DialogContent title={path} description={`Versions of this memory file as the agent wrote them.`} side="right">
      {versions.length === 0 ? <p className="text-sm text-muted">No versions recorded.</p> : (
        <>
          <div role="group" aria-label="Versions" className="mb-2 flex flex-wrap gap-1">
            {versions.map((v) => (
              <button key={v.version} type="button" aria-pressed={v.version === version} onClick={() => setVersion(v.version)}
                className={`mono rounded border px-2 py-0.5 text-xs ${v.version === version ? "border-memory bg-memory-soft" : "border-line"}`}>
                v{v.version}{v.deleted ? " (deleted)" : ""} · step {v.step}
              </button>
            ))}
          </div>
          <div role="group" aria-label="View" className="mb-2 flex gap-1">
            {(["content", "diff"] as const).map((v) => (
              <button key={v} type="button" aria-pressed={view === v} disabled={v === "diff" && !previous} onClick={() => setView(v)}
                className={`rounded border px-2 py-0.5 text-xs disabled:opacity-50 ${view === v ? "border-memory bg-memory-soft" : "border-line"}`}>{v}</button>
            ))}
          </div>
          {current?.deleted ? <p className="mb-2 text-sm text-warn">This version is the deletion of the file.</p> : null}
          {body.error ? <p role="alert" className="text-sm text-bad">{body.error}</p> : null}
          {/* plain text in a <pre>: a memory file is attacker-influenced and is never rendered as markup */}
          <pre className="mono max-h-[60vh] overflow-auto whitespace-pre-wrap break-words border border-line bg-surface p-2" tabIndex={0} aria-label={`${path} ${view}`}>{body.text ?? ""}</pre>
        </>
      )}
    </DialogContent>
  );
}
