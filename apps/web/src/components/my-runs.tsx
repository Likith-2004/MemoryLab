"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ApiProblem, api, describe } from "@/lib/client";
import type { Run, Visibility } from "@/lib/types";
import { Banner, Button, StatusChip } from "./ui";
import { Dialog, DialogContent } from "./dialog";

export function MyRuns() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (before: string | null) => {
    try {
      const page = await api<{ runs: Run[]; next: string | null }>(`/runs?limit=25${before ? `&before=${encodeURIComponent(before)}` : ""}`);
      setRuns((prev) => (before && prev ? prev.concat(page.runs) : page.runs));
      setNext(page.next);
    } catch (e) { setError(describe(e)); }
  }, []);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- the first page loads once on mount
  useEffect(() => { void load(null); }, [load]);

  async function setVisibility(run: Run, visibility: Visibility) {
    setError(null);
    try {
      const updated = await api<Run>(`/runs/${run.id}`, { method: "PATCH", body: { visibility } });
      setRuns((rs) => rs?.map((r) => (r.id === run.id ? updated : r)) ?? null);
    } catch (e) {
      setError(e instanceof ApiProblem && e.code === "benchmark_patch_invalid" ? "A run on the leaderboard cannot be made private." : describe(e));
    }
  }
  async function remove(run: Run) {
    setBusy(true);
    try { await api(`/runs/${run.id}`, { method: "DELETE" }); setRuns((rs) => rs?.filter((r) => r.id !== run.id) ?? null); setConfirm(null); }
    catch (e) { setError(describe(e)); setConfirm(null); } finally { setBusy(false); }
  }

  return (
    <section aria-labelledby="runs-h">
      <div className="mb-3 flex items-center justify-between"><h1 id="runs-h" className="text-xl font-semibold">My runs</h1><Link href="/runs/new" className="rounded border border-ink bg-ink px-3 py-1.5 text-surface no-underline">New run</Link></div>
      {error ? <Banner tone="bad">{error}</Banner> : null}
      {runs === null && !error ? <p role="status">Loading…</p> : null}
      {runs && runs.length === 0 ? <p className="border border-line bg-panel p-4">You have not started a run yet. <Link href="/runs/new">Start one</Link>.</p> : null}
      {runs && runs.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full border border-line bg-panel text-left text-sm">
            <caption className="sr-only">Your runs, newest first</caption>
            <thead><tr className="border-b border-line"><th scope="col" className="p-2">Run</th><th scope="col" className="p-2">Status</th><th scope="col" className="p-2">Model</th><th scope="col" className="p-2">Steps</th><th scope="col" className="p-2">Visibility</th><th scope="col" className="p-2"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-b border-line">
                  <td className="p-2"><Link href={`/runs/${r.id}`}>{r.title ?? r.id.slice(0, 8)}</Link></td>
                  <td className="p-2"><StatusChip status={r.status} /></td>
                  <td className="mono p-2">{r.model_id ?? "–"}</td><td className="mono p-2">{r.steps}</td>
                  <td className="p-2"><label className="sr-only" htmlFor={`vis-${r.id}`}>Visibility of {r.title ?? r.id.slice(0, 8)}</label>
                    <select id={`vis-${r.id}`} className="rounded border border-line bg-panel px-1 py-0.5" value={r.visibility} onChange={(e) => void setVisibility(r, e.target.value as Visibility)}>
                      <option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option></select></td>
                  <td className="p-2"><Button variant="danger" onClick={() => setConfirm(r)} aria-label={`Delete ${r.title ?? r.id.slice(0, 8)}`}>Delete</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {next ? <p className="mt-3"><Button variant="plain" onClick={() => void load(next)}>Load more</Button></p> : null}
      <Dialog open={confirm !== null} onOpenChange={(o) => { if (!o) setConfirm(null); }}>
        {confirm ? (
          <DialogContent title="Delete this run?" description="It disappears at once; its files are purged after 7 days.">
            <p className="mb-3 text-sm">{confirm.title ?? confirm.id}</p>
            <div className="flex gap-2"><Button variant="danger" disabled={busy} onClick={() => void remove(confirm)}>Delete run</Button><Button variant="plain" onClick={() => setConfirm(null)}>Cancel</Button></div>
          </DialogContent>
        ) : null}
      </Dialog>
    </section>
  );
}
