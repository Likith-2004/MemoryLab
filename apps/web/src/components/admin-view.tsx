"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ApiProblem, api, describe } from "@/lib/client";
import type { AdminReport } from "@/lib/types";
import { Banner, Button, Field, inputClass } from "./ui";

interface Snapshot { queued: string[]; active: string[]; breaker: { open: boolean; reason: string } }

/** Operator screen. Hiding it from non-admins is a convenience: every call below is re-checked by the API against the database role and the live session. */
export function AdminView() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [reports, setReports] = useState<AdminReport[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([api<Snapshot>("/admin/runs"), api<{ reports: AdminReport[] }>("/admin/reports?status=open&limit=50")]);
      setSnap(s); setReports(r.reports); setError(null);
    } catch (e) { if (e instanceof ApiProblem && e.status === 404) setDenied(true); else setError(describe(e)); }
  }, []);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- loads once on mount
  useEffect(() => { void load(); }, [load]);

  async function act(label: string, fn: () => Promise<unknown>) {
    setNotice(null); setError(null);
    try { await fn(); setNotice(`${label}: done`); await load(); } catch (e) { setError(`${label}: ${describe(e)}`); }
  }
  const needReason = reason.trim().length === 0;

  if (denied) return <p className="border border-line bg-panel p-4">Not found.</p>;
  return (
    <section aria-labelledby="adm-h">
      <h1 id="adm-h" className="mb-3 text-xl font-semibold">Admin</h1>
      {snap?.breaker.open ? <Banner tone="bad" role="alert">The breaker is OPEN: new runs are refused. Reason: {snap.breaker.reason}</Banner> : null}
      {error ? <Banner tone="bad">{error}</Banner> : null}
      {notice ? <Banner tone="ok">{notice}</Banner> : null}
      <Field id="admin-reason" label="Reason (required for every action, and recorded in the audit log)"><input id="admin-reason" className={inputClass} maxLength={200} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>

      <h2 className="mb-1 mt-4 text-base font-semibold">Breaker</h2>
      <div className="mb-4 flex gap-2">
        <Button variant="danger" disabled={needReason || snap?.breaker.open === true} onClick={() => void act("Open breaker", () => api("/admin/breaker", { method: "POST", body: { open: true, reason } }))}>Open breaker</Button>
        <Button variant="plain" disabled={needReason || snap?.breaker.open !== true} onClick={() => void act("Close breaker", () => api("/admin/breaker", { method: "POST", body: { open: false, reason } }))}>Close breaker</Button>
      </div>

      <h2 className="mb-1 text-base font-semibold">Live and queued runs</h2>
      {snap && snap.active.length + snap.queued.length === 0 ? <p className="mb-4 text-sm text-muted">Nothing is running or queued.</p> : null}
      <ul className="mb-4">{[...(snap?.active ?? []).map((id) => ({ id, kind: "active" })), ...(snap?.queued ?? []).map((id) => ({ id, kind: "queued" }))].map((r) => (
        <li key={r.id} className="flex items-center gap-2 border-b border-line py-1"><Link href={`/runs/${r.id}`} className="mono">{r.id.slice(0, 8)}</Link><span className="text-xs text-muted">{r.kind}</span>
          <Button variant="danger" disabled={needReason} onClick={() => void act("Kill run", () => api(`/admin/runs/${r.id}/kill`, { method: "POST", body: { reason } }))}>Kill</Button></li>
      ))}</ul>

      <h2 className="mb-1 text-base font-semibold">Open reports</h2>
      {reports.length === 0 ? <p className="text-sm text-muted">No open reports.</p> : null}
      <ul>{reports.map((r) => (
        <li key={r.id} className="mb-2 border border-line bg-panel p-2">
          <p className="text-sm"><Link href={`/runs/${r.run_id}`}>{r.run_title ?? r.run_id.slice(0, 8)}</Link> <span className="mono text-xs text-muted">{r.run_visibility} · {r.run_status}{r.run_deleted ? " · deleted" : ""}</span></p>
          {/* the reporter's words, as text */}
          <p className="mono my-1 whitespace-pre-wrap break-words text-xs">{r.reason}</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="plain" disabled={needReason} onClick={() => void act("Action report", () => api(`/admin/reports/${r.id}`, { method: "PATCH", body: { status: "actioned", reason } }))}>Mark actioned</Button>
            <Button variant="plain" disabled={needReason} onClick={() => void act("Dismiss report", () => api(`/admin/reports/${r.id}`, { method: "PATCH", body: { status: "dismissed", reason } }))}>Dismiss</Button>
            <Button variant="danger" disabled={needReason} onClick={() => void act("Unverify run", () => api(`/admin/leaderboard/${r.run_id}/unverify`, { method: "POST", body: { reason } }))}>Remove from leaderboard</Button>
          </div>
        </li>
      ))}</ul>
    </section>
  );
}
