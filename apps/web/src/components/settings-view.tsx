"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiProblem, api, describe, session } from "@/lib/client";
import type { Me } from "@/lib/types";
import { Banner, Button, Field, inputClass } from "./ui";

export function SettingsView({ me }: { me: Me }) {
  const router = useRouter();
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function everywhere() {
    setBusy(true);
    try { await session("logout", { everywhere: true }); router.replace("/"); router.refresh(); } catch (e) { setError(describe(e)); } finally { setBusy(false); }
  }
  async function erase(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api("/me", { method: "DELETE", body: { confirm } });
      await session("logout", {}).catch(() => undefined);
      router.replace("/"); router.refresh();
    } catch (err) {
      setError(err instanceof ApiProblem && err.code === "runs_still_active" ? "Your run is still stopping. Try again in a few seconds." : describe(err));
      setNote(null);
    } finally { setConfirm(""); setBusy(false); }
  }

  return (
    <section aria-labelledby="set-h" className="max-w-xl">
      <h1 id="set-h" className="mb-3 text-xl font-semibold">Account</h1>
      {error ? <Banner tone="bad">{error}</Banner> : null}
      {note ? <Banner tone="ok">{note}</Banner> : null}
      <dl className="mb-6 grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted">Account id</dt><dd className="mono break-all">{me.id}</dd>
        <dt className="text-muted">Role</dt><dd>{me.role}</dd>
        <dt className="text-muted">Active runs</dt><dd className="mono">{me.active_runs} of {me.limits.concurrent_runs}</dd>
        <dt className="text-muted">Runs per hour</dt><dd className="mono">{me.limits.runs_per_hour}</dd>
      </dl>
      <h2 className="mb-1 text-base font-semibold">Sessions</h2>
      <p className="mb-2 text-sm text-muted">Signing out everywhere ends every device&apos;s session, including this one.</p>
      <Button variant="plain" disabled={busy} onClick={() => void everywhere()}>Sign out everywhere</Button>
      <h2 className="mb-1 mt-8 text-base font-semibold">Delete account</h2>
      <p className="mb-2 text-sm">This deletes your private and unlisted runs. Verified public benchmark runs stay on the leaderboard with no link to you. It cannot be undone.</p>
      <form onSubmit={erase}>
        <Field id="confirm-delete" label="Type DELETE to confirm"><input id="confirm-delete" className={inputClass} autoComplete="off" value={confirm} onChange={(e) => setConfirm(e.target.value)} /></Field>
        <Button type="submit" variant="danger" disabled={busy || confirm !== "DELETE"}>Delete my account</Button>
      </form>
    </section>
  );
}
