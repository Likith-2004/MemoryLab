"use client";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiProblem, api, describe } from "@/lib/client";
import type { Game, Models, Preset, Visibility } from "@/lib/types";
import { Banner, Button, Field, inputClass } from "./ui";
import { CapsFields, KeyField, capsValid, type Caps } from "./key-field";

const STEPS = ["Mode", "Game", "Model and preset", "Caps and key"] as const;
type Mode = "demo" | "benchmark";

interface Validated { config_hash: string; mode: string; benchmark_eligible: boolean }

export function NewRun({ capacityNote }: { capacityNote?: string }) {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [catalog, setCatalog] = useState<{ games: Game[]; presets: Preset[]; models: Models } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("demo");
  const [game, setGame] = useState("");
  const [provider, setProvider] = useState("");
  const [modelId, setModelId] = useState("");
  const [presetRef, setPresetRef] = useState("general@1");
  const [custom, setCustom] = useState(false);
  const [customText, setCustomText] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("private");
  const [caps, setCaps] = useState<Caps>({ max_steps: 500, max_tokens: 500_000, max_wall_s: 1800 });
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ApiProblem | null>(null);
  const [validated, setValidated] = useState<Validated | null>(null);

  useEffect(() => {
    Promise.all([api<Game[]>("/games"), api<Preset[]>("/presets"), api<Models>("/models")]).then(([games, presets, models]) => {
      setCatalog({ games, presets, models });
      const p = Object.keys(models.providers)[0] ?? "";
      setGame(games[0]?.name ?? ""); setProvider(p); setModelId(models.providers[p]?.[0] ?? "");
    }).catch((e: unknown) => setLoadError(describe(e)));
  }, []);

  const preset = catalog?.presets.find((p) => p.ref === presetRef);
  const body = () => ({
    config: { provider, model_id: modelId, preset_ref: custom ? "custom@0" : presetRef, mode },
    caps, ...(custom ? { preset_text: customText } : {}),
  });

  async function review() {
    setBusy(true); setProblem(null);
    try {
      setValidated(await api<Validated>("/runs/validate", { method: "POST", body: body() }));
      setStep(4);
    } catch (e) {
      setProblem(e instanceof ApiProblem ? e : null);
      setValidated(null);
    } finally { setBusy(false); }
  }

  async function launch(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setProblem(null);
    try {
      const out = await api<{ run_id: string }>("/runs", { method: "POST", body: { game, visibility, api_key: key, ...body() } });
      router.push(`/runs/${out.run_id}`);
    } catch (err) {
      setProblem(err instanceof ApiProblem ? err : null);
    } finally {
      setKey(""); // cleared after every submit, successful or not
      setBusy(false);
    }
  }

  if (loadError) return <Banner tone="bad">{loadError}</Banner>;
  if (!catalog) return <p role="status">Loading…</p>;
  if (catalog.games.length === 0) return <Banner tone="warn">No games are available yet.</Banner>;
  const modelsForProvider = catalog.models.providers[provider] ?? [];
  const approved = catalog.presets;
  const stepOk = [true, game !== "", modelId !== "" && (!custom || customText.trim().length > 0), capsValid(caps)][step] ?? true;

  return (
    <section aria-labelledby="new-h" className="max-w-2xl">
      <h1 id="new-h" className="mb-1 text-xl font-semibold">New run</h1>
      <ol className="mb-3 flex flex-wrap gap-2 text-xs" aria-label="Steps">
        {STEPS.map((s, i) => <li key={s} aria-current={i === step ? "step" : undefined} className={`rounded border px-2 py-0.5 ${i === step ? "border-ink font-semibold" : "border-line text-muted"}`}>{i + 1}. {s}</li>)}
        <li aria-current={step === 4 ? "step" : undefined} className={`rounded border px-2 py-0.5 ${step === 4 ? "border-ink font-semibold" : "border-line text-muted"}`}>5. Review</li>
      </ol>
      {capacityNote ? <Banner tone="info">{capacityNote}</Banner> : null}
      {problem ? <ProblemBanner problem={problem} onRetry={step === 4 ? undefined : undefined} /> : null}

      {step === 0 ? (
        <fieldset className="border border-line p-3"><legend className="px-1 text-sm font-medium">Mode</legend>
          {([["demo", "Demo", "Any config, any preset (including your own instructions). Not ranked."],
             ["benchmark", "Benchmark", "Approved presets only, one changed field when you fork. Eligible for the leaderboard after verification."]] as const).map(([v, t, d]) => (
            <label key={v} className="mb-2 flex gap-2"><input type="radio" name="mode" checked={mode === v} onChange={() => { setMode(v); if (v === "benchmark") setCustom(false); }} />
              <span><span className="font-medium">{t}</span><br /><span className="text-sm text-muted">{d}</span></span></label>
          ))}
        </fieldset>
      ) : null}

      {step === 1 ? (
        <Field id="game" label="Game" hint="Only games with a public licence are listed.">
          <select id="game" className={inputClass} value={game} onChange={(e) => setGame(e.target.value)}>
            {catalog.games.map((g) => <option key={g.id} value={g.name}>{g.name} ({g.licence})</option>)}
          </select>
        </Field>
      ) : null}

      {step === 2 ? (
        <>
          <Field id="provider" label="Provider"><select id="provider" className={inputClass} value={provider} onChange={(e) => { setProvider(e.target.value); setModelId(catalog.models.providers[e.target.value]?.[0] ?? ""); }}>
            {Object.keys(catalog.models.providers).map((p) => <option key={p}>{p}</option>)}</select></Field>
          <Field id="model" label="Model"><select id="model" className={inputClass} value={modelId} onChange={(e) => setModelId(e.target.value)}>
            {modelsForProvider.map((m) => <option key={m}>{m}</option>)}</select></Field>
          <Field id="preset" label="Memory preset" hint="The full instruction text is shown below: what the agent is told about its memory.">
            <select id="preset" className={inputClass} value={custom ? "custom" : presetRef} onChange={(e) => { if (e.target.value === "custom") setCustom(true); else { setCustom(false); setPresetRef(e.target.value); } }}>
              {approved.map((p) => <option key={p.ref} value={p.ref}>{p.label}{p.is_adaptation ? " (adaptation)" : ""}</option>)}
              {mode === "demo" ? <option value="custom">Custom instructions (Demo only)</option> : null}
            </select>
          </Field>
          {custom ? (
            <Field id="custom-text" label="Your instructions" hint="Plain text, up to 4000 characters. Demo runs only.">
              <textarea id="custom-text" className={`${inputClass} mono`} rows={6} maxLength={4000} value={customText} onChange={(e) => setCustomText(e.target.value)} />
            </Field>
          ) : preset ? <pre className="mono mb-3 max-h-56 overflow-auto whitespace-pre-wrap border border-line bg-surface p-2 text-xs" tabIndex={0} aria-label="Preset text">{preset.text}</pre> : null}
          <Field id="visibility" label="Who can see this run"><select id="visibility" className={inputClass} value={visibility} onChange={(e) => setVisibility(e.target.value as Visibility)}>
            <option value="private">Only me</option><option value="unlisted">Anyone with the link</option><option value="public">Public</option></select></Field>
        </>
      ) : null}

      {step === 3 ? <CapsFields caps={caps} onChange={setCaps} /> : null}

      {step === 4 ? (
        <form onSubmit={launch}>
          <h2 className="mb-2 text-base font-semibold">Review</h2>
          <dl className="mb-3 grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-muted">Mode</dt><dd>{mode}</dd><dt className="text-muted">Game</dt><dd>{game}</dd>
            <dt className="text-muted">Model</dt><dd className="mono">{provider} / {modelId}</dd>
            <dt className="text-muted">Preset</dt><dd>{custom ? "custom instructions" : presetRef}</dd>
            <dt className="text-muted">Caps</dt><dd className="mono">{caps.max_steps} steps · {caps.max_tokens.toLocaleString()} tokens · {caps.max_wall_s} s</dd>
            <dt className="text-muted">Visibility</dt><dd>{visibility}</dd>
            <dt className="text-muted">Config hash</dt><dd className="mono break-all">{validated?.config_hash}</dd>
          </dl>
          <KeyField value={key} onChange={setKey} error={problem?.code === "key_rejected" ? describe(problem) : null} />
          <Button type="submit" disabled={busy || key.length < 8}>{busy ? "Starting…" : "Start run"}</Button>
        </form>
      ) : null}

      <div className="mt-4 flex gap-2">
        {step > 0 ? <Button variant="plain" onClick={() => { setStep(step - 1); setProblem(null); }}>Back</Button> : null}
        {step < 3 ? <Button disabled={!stepOk} onClick={() => setStep(step + 1)}>Next</Button> : null}
        {step === 3 ? <Button disabled={!stepOk || busy} onClick={review}>{busy ? "Checking…" : "Review"}</Button> : null}
      </div>
    </section>
  );
}

function ProblemBanner({ problem }: { problem: ApiProblem; onRetry?: () => void }) {
  if (problem.code === "key_rejected") return null; // shown on the key field itself
  const retry = problem.retryAfter ? ` Retry in about ${problem.retryAfter >= 120 ? `${Math.round(problem.retryAfter / 60)} minutes` : `${problem.retryAfter} seconds`}.` : "";
  const text = problem.code === "capacity_full" ? `The lab is full right now.${retry}` : problem.code === "benchmark_patch_invalid" ? "Benchmark mode uses an approved preset." : problem.code === "rate_limited" && /active run/i.test(problem.title) ? "You already have a run in progress. Stop it or wait for it to end." : describe(problem);
  return <Banner tone={problem.code === "capacity_full" ? "warn" : "bad"}>{text}</Banner>;
}
