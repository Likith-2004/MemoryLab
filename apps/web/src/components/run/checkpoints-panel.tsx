"use client";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiProblem, api, describe } from "@/lib/client";
import type { Checkpoint, Models, Preset } from "@/lib/types";
import { Banner, Button, Field, inputClass } from "../ui";
import { CapsFields, KeyField, capsValid, type Caps } from "../key-field";
import { Dialog, DialogContent } from "../dialog";

export function CheckpointsPanel({ runId, canFork, forkEnabled, signedIn }: { runId: string; canFork: boolean; forkEnabled: boolean; signedIn: boolean }) {
  const [rows, setRows] = useState<Checkpoint[] | null>(null);
  const [forking, setForking] = useState<Checkpoint | null>(null);
  useEffect(() => { api<Checkpoint[]>(`/runs/${runId}/checkpoints`).then(setRows).catch(() => setRows([])); }, [runId]);
  return (
    <section aria-labelledby="cp-h" className="border border-line bg-panel">
      <h2 id="cp-h" className="border-b border-line px-2 py-1 text-sm font-semibold">Checkpoints</h2>
      {rows && rows.length === 0 ? <p className="p-3 text-sm text-muted">No checkpoints yet.</p> : null}
      <ul>
        {(rows ?? []).map((c) => (
          <li key={c.id} className="flex items-center justify-between gap-2 border-b border-line px-2 py-1">
            <span className="mono">step {c.step} · memory v{c.memory_version}</span>
            {canFork && signedIn ? <Button variant="plain" disabled={!forkEnabled} onClick={() => setForking(c)} aria-label={`Fork from step ${c.step}`}>Fork</Button> : null}
          </li>
        ))}
      </ul>
      {!forkEnabled ? <p className="p-2 text-xs text-muted">Forking is not available yet on this deployment.</p> : null}
      {canFork && !signedIn ? <p className="p-2 text-xs text-muted">Sign in to fork a run.</p> : null}
      <Dialog open={forking !== null} onOpenChange={(o) => { if (!o) setForking(null); }}>
        {forking ? <ForkForm runId={runId} checkpoint={forking} /> : null}
      </Dialog>
    </section>
  );
}

function ForkForm({ runId, checkpoint }: { runId: string; checkpoint: Checkpoint }) {
  const router = useRouter();
  const [parent, setParent] = useState<{ config: Record<string, unknown>; mode: string } | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [models, setModels] = useState<Models | null>(null);
  const [patch, setPatch] = useState<Record<string, string>>({});
  const [caps, setCaps] = useState<Caps>({ max_steps: 500, max_tokens: 500_000, max_wall_s: 1800 });
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; key?: boolean } | null>(null);

  useEffect(() => {
    void Promise.all([api<{ config: Record<string, unknown>; mode: string }>(`/runs/${runId}/config`), api<Preset[]>("/presets"), api<Models>("/models")])
      .then(([c, p, m]) => {
        setParent(c); setPresets(p); setModels(m);
        const pc = (c.config.caps ?? {}) as Partial<Caps>;
        setCaps({ max_steps: pc.max_steps ?? 500, max_tokens: pc.max_tokens ?? 500_000, max_wall_s: Math.trunc(pc.max_wall_s ?? 1800) });
      }).catch((e: unknown) => setError({ text: describe(e) }));
  }, [runId]);

  const changed = Object.entries(patch).filter(([k, v]) => v !== String(parent?.config[k] ?? ""));
  const benchmark = parent?.mode === "benchmark";
  const locked = (field: string) => benchmark && changed.length >= 1 && !changed.some(([k]) => k === field);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!parent || !capsValid(caps)) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ run_id: string }>(`/runs/${runId}/fork`, { method: "POST", body: {
        checkpoint_id: checkpoint.id, config_patch: Object.fromEntries(changed), caps, api_key: key } });
      router.push(`/runs/${out.run_id}`);
    } catch (err) {
      setError({ text: err instanceof ApiProblem && err.code === "benchmark_patch_invalid" ? "A benchmark fork may change one field only." : describe(err), key: err instanceof ApiProblem && err.code === "key_rejected" });
    } finally {
      setKey("");
      setBusy(false);
    }
  }

  const presetText = presets.find((p) => p.ref === (patch.preset_ref ?? String(parent?.config.preset_ref ?? "")))?.text;
  return (
    <DialogContent title={`Fork from step ${checkpoint.step}`} description="A new run that starts from this checkpoint. The change you make is stored and shown on the new run.">
      {!parent && !error ? <p role="status">Loading the parent configuration…</p> : null}
      {error ? <Banner tone="bad">{error.text}</Banner> : null}
      {parent ? (
        <form onSubmit={submit}>
          <p className="mb-2 text-sm">Mode: <span className="mono">{parent.mode}</span>{benchmark ? ". A benchmark fork may change one field only." : ""}</p>
          <Field id="fork-preset" label="Preset">
            <select id="fork-preset" className={inputClass} value={patch.preset_ref ?? String(parent.config.preset_ref ?? "")} disabled={locked("preset_ref")}
              onChange={(e) => setPatch({ ...patch, preset_ref: e.target.value })}>
              {presets.map((p) => <option key={p.ref} value={p.ref}>{p.label}</option>)}
            </select>
          </Field>
          {presetText ? <pre className="mono mb-3 max-h-40 overflow-auto whitespace-pre-wrap border border-line bg-surface p-2 text-xs" tabIndex={0} aria-label="Preset text">{presetText}</pre> : null}
          <Field id="fork-model" label="Model">
            <select id="fork-model" className={inputClass} value={patch.model_id ?? String(parent.config.model_id ?? "")} disabled={locked("model_id")}
              onChange={(e) => setPatch({ ...patch, model_id: e.target.value })}>
              {Object.values(models?.providers ?? {}).flat().map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </Field>
          <p className="mb-3 text-xs text-muted">{changed.length === 0 ? "Nothing changed yet." : `Changes: ${changed.map(([k, v]) => `${k} → ${v}`).join(", ")}`}</p>
          <CapsFields caps={caps} onChange={setCaps} />
          <KeyField value={key} onChange={setKey} error={error?.key ? error.text : null} />
          <Button type="submit" disabled={busy || !capsValid(caps) || key.length < 8}>{busy ? "Starting…" : "Start fork"}</Button>
        </form>
      ) : null}
    </DialogContent>
  );
}
