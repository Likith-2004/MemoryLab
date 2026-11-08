"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, describe } from "@/lib/client";
import type { Run, Visibility } from "@/lib/types";
import { Banner, Button, Field, inputClass } from "./ui";

interface Row { id: string; title: string; variable: string; visibility: Visibility; n_runs: number }
const VARIABLES = ["preset_ref", "model_id", "memory", "inject_path", "inject_cap_chars", "observation", "screenshots", "caps", "mode"];

export function ExperimentsList() {
  const router = useRouter();
  const [rows, setRows] = useState<Row[] | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [variable, setVariable] = useState("preset_ref");
  const [chosen, setChosen] = useState<string[]>([]);
  const [visibility, setVisibility] = useState<Visibility>("private");
  const [busy, setBusy] = useState(false);

  useEffect(() => { void Promise.all([api<Row[]>("/experiments"), api<{ runs: Run[] }>("/runs?limit=100")]).then(([r, p]) => { setRows(r); setRuns(p.runs); }).catch((e: unknown) => setError(describe(e))); }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const out = await api<{ id: string }>("/experiments", { method: "POST", body: { title, variable, run_ids: chosen, visibility } });
      router.push(`/experiments/${out.id}`);
    } catch (err) { setError(describe(err)); } finally { setBusy(false); }
  }

  return (
    <section aria-labelledby="exps-h">
      <h1 id="exps-h" className="mb-3 text-xl font-semibold">Experiments</h1>
      {error ? <Banner tone="bad">{error}</Banner> : null}
      {rows && rows.length === 0 ? <p className="mb-4 border border-line bg-panel p-4">No experiments yet. Group two or more of your runs that differ in one setting.</p> : null}
      <ul className="mb-6">{(rows ?? []).map((r) => <li key={r.id} className="border-b border-line py-1"><Link href={`/experiments/${r.id}`}>{r.title}</Link> <span className="mono text-xs text-muted">{r.variable} · {r.n_runs} runs · {r.visibility}</span></li>)}</ul>
      <form onSubmit={create} className="max-w-xl border border-line bg-panel p-3">
        <h2 className="mb-2 text-base font-semibold">New experiment</h2>
        <Field id="exp-title" label="Title"><input id="exp-title" className={inputClass} required maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
        <Field id="exp-var" label="The one variable the runs differ in" hint="The runs must be identical in every other setting; the server checks."><select id="exp-var" className={inputClass} value={variable} onChange={(e) => setVariable(e.target.value)}>{VARIABLES.map((v) => <option key={v}>{v}</option>)}</select></Field>
        <fieldset className="mb-3 border border-line p-2"><legend className="px-1 text-sm font-medium">Your runs</legend>
          {runs.length === 0 ? <p className="text-sm text-muted">You have no runs yet.</p> : runs.map((r) => (
            <label key={r.id} className="flex items-center gap-2 py-0.5 text-sm"><input type="checkbox" checked={chosen.includes(r.id)} onChange={(e) => setChosen(e.target.checked ? [...chosen, r.id] : chosen.filter((x) => x !== r.id))} />
              <span>{r.title ?? r.id.slice(0, 8)}</span><span className="mono text-xs text-muted">{r.model_id} · {r.status}</span></label>
          ))}
        </fieldset>
        <Field id="exp-vis" label="Who can see it"><select id="exp-vis" className={inputClass} value={visibility} onChange={(e) => setVisibility(e.target.value as Visibility)}><option value="private">Only me</option><option value="unlisted">Anyone with the link</option><option value="public">Public</option></select></Field>
        <Button type="submit" disabled={busy || chosen.length === 0 || title === ""}>{busy ? "Creating…" : "Create experiment"}</Button>
      </form>
    </section>
  );
}
