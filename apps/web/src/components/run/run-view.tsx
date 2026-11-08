"use client";
import * as Tabs from "@radix-ui/react-tabs";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ApiProblem, api, describe } from "@/lib/client";
import { derive, toRows } from "@/lib/runmodel";
import { FINAL_STATES, type Run, type RunStatus } from "@/lib/types";
import { Banner, Button, StatusChip } from "../ui";
import { BudgetPanel } from "./budget-panel";
import { CheckpointsPanel } from "./checkpoints-panel";
import { GameFrame } from "./game-frame";
import { LogPanel } from "./log-panel";
import { MemoryPanel } from "./memory-panel";
import { Timeline } from "./timeline";
import { useRunEvents } from "./use-run-events";

export interface Viewer { id: string; role: "user" | "admin" }

const STATE_NOTE: Record<string, string> = {
  queued: "Queued: waiting for a free slot.",
  starting: "Starting: preparing the sandbox.",
  session_boundary: "Session boundary: the model's context is being reset; memory is kept.",
  dreaming: "Dreaming: the agent is reorganising its memory between sessions.",
  stopping: "Stopping after the current step.",
};

export function RunView({ run, viewer, wsBase, forkEnabled, initialAt }: { run: Run; viewer: Viewer | null; wsBase: string; forkEnabled: boolean; initialAt: number | null }) {
  const startsFinal = FINAL_STATES.includes(run.status);
  const { events, connection, loaded } = useRunEvents({ runId: run.id, live: !startsFinal, wsBase });
  const [at, setAt] = useState<number | null>(initialAt);
  const [group, setGroup] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [notice, setNotice] = useState<{ tone: "bad" | "ok"; text: string } | null>(null);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- reading a browser-only preference after hydration
  useEffect(() => { try { setGroup(localStorage.getItem("ml.group") === "1"); } catch { /* storage unavailable */ } }, []);

  const full = useMemo(() => derive(events), [events]);
  const cursor = at ?? full.step;
  const view = useMemo(() => derive(events, at ?? undefined), [events, at]);
  const rows = useMemo(() => toRows(events, group, at ?? undefined), [events, group, at]);
  const status: RunStatus = (loaded && view.state ? view.state : run.status);
  const ended = FINAL_STATES.includes(status);
  const replaying = at !== null || (ended && loaded);
  const canControl = viewer !== null && (viewer.id === run.owner_id || viewer.role === "admin");
  const fresh = connection === "live" && !replaying ? (events.at(-1)?.seq ?? null) : null;

  function scrub(step: number) {
    setAt(step);
    const url = new URL(window.location.href);
    url.searchParams.set("at", String(step));
    window.history.replaceState(null, "", url);
  }
  function follow() {
    setAt(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("at");
    window.history.replaceState(null, "", url);
  }

  async function stop() {
    setStopping(true);
    setNotice(null);
    try {
      await api(`/runs/${run.id}/stop`, { method: "POST" });
      setNotice({ tone: "ok", text: "Stop requested: the run ends after the current step." });
    } catch (e) {
      setNotice({ tone: "bad", text: describe(e) });
    } finally {
      setStopping(false);
    }
  }
  async function exportBundle() {
    setNotice(null);
    try {
      const out = await api<{ url: string; sha256: string }>(`/runs/${run.id}/export`);
      window.location.assign(out.url);
      setNotice({ tone: "ok", text: `Download started. SHA-256: ${out.sha256}` });
    } catch (e) {
      setNotice({ tone: "bad", text: e instanceof ApiProblem && e.code === "export_not_ready" ? "The run has not finished and been sealed yet." : describe(e) });
    }
  }

  const frameStep = (() => { for (let i = events.length - 1; i >= 0; i--) { const e = events[i]!; if (e.type === "obs" && (at === null || e.step <= at)) return e.step; } return null; })();
  const reasonText = view.reason ? ` (${view.reason})` : "";

  return (
    <div>
      <div className="sticky top-0 z-10 mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 border border-line bg-panel px-3 py-2" role="region" aria-label="Run status">
        <h1 className="text-base font-semibold break-words">{run.title ?? `Run ${run.id.slice(0, 8)}`}</h1>
        <StatusChip status={status} />
        <span className="mono text-xs">step {cursor} / {run.caps.max_steps}</span>
        <span className="mono text-xs">tokens {view.tokens.toLocaleString()} / {run.caps.max_tokens.toLocaleString()}</span>
        <span className="mono text-xs">score {view.score}</span>
        <span className="ml-auto flex items-center gap-2">
          {ended ? <Button variant="plain" onClick={exportBundle}>Export</Button> : null}
          {canControl && !ended ? <Button variant="danger" disabled={stopping || status === "stopping"} onClick={stop}>Stop run</Button> : null}
        </span>
      </div>

      <div aria-live="polite">
        {notice ? <Banner tone={notice.tone}>{notice.text}</Banner> : null}
        {!ended && STATE_NOTE[status] ? <Banner tone="info">{STATE_NOTE[status]}</Banner> : null}
        {ended ? <Banner tone={status === "failed" || status === "killed" ? "bad" : "info"}>Run {status}{reasonText}.</Banner> : null}
        {connection === "resuming" ? <Banner tone="warn">Disconnected. Resuming the live view…</Banner> : null}
        {connection === "polling" ? <Banner tone="warn">The live connection is unavailable; updating every few seconds.</Banner> : null}
        {connection === "failed" ? <Banner tone="bad">Could not load this run&apos;s events. Reload to try again.</Banner> : null}
        {!loaded && connection === "loading" ? <p role="status" className="mb-2 text-sm text-muted">Loading the event log…</p> : null}
        {at !== null && !ended ? <Banner tone="info">Viewing step {at}. <Button variant="plain" onClick={follow}>Back to live</Button></Banner> : null}
      </div>

      <div className="grid gap-3 md:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <div>
          <GameFrame runId={run.id} step={frameStep} />
          <Timeline max={Math.max(full.step, run.steps)} value={cursor} onChange={scrub} memoryTicks={full.memoryTicks} boundaries={full.boundaries} disabled={!loaded} />
          {replaying ? <p className="mt-1 text-xs text-muted">Replay: drag or use the arrow keys; [ and ] jump between memory changes.</p> : null}
        </div>

        <Tabs.Root defaultValue="log" className="min-w-0">
          <Tabs.List aria-label="Run panels" className="mb-2 flex gap-1 md:hidden">
            {[["log", "Log"], ["memory", "Memory"], ["budget", "Budget"], ["checkpoints", "Checkpoints"]].map(([v, l]) => (
              <Tabs.Trigger key={v} value={v!} className="rounded border border-line px-2 py-1 text-sm data-[state=active]:border-ink data-[state=active]:bg-ink data-[state=active]:text-surface">{l}</Tabs.Trigger>
            ))}
          </Tabs.List>
          <div className="grid gap-3 lg:grid-cols-2">
            <Tabs.Content value="log" forceMount className="min-w-0 data-[state=inactive]:hidden md:data-[state=inactive]:block"><LogPanel rows={rows} fresh={fresh} group={group}
              onGroup={(v) => { setGroup(v); try { localStorage.setItem("ml.group", v ? "1" : "0"); } catch { /* ignore */ } }} /></Tabs.Content>
            <Tabs.Content value="memory" forceMount className="min-w-0 data-[state=inactive]:hidden md:data-[state=inactive]:block"><MemoryPanel runId={run.id} atStep={cursor} events={events} /></Tabs.Content>
            <Tabs.Content value="budget" forceMount className="min-w-0 data-[state=inactive]:hidden md:data-[state=inactive]:block"><BudgetPanel run={run} step={cursor} tokens={view.tokens} wallS={view.wallS} /></Tabs.Content>
            <Tabs.Content value="checkpoints" forceMount className="min-w-0 data-[state=inactive]:hidden md:data-[state=inactive]:block"><CheckpointsPanel runId={run.id} canFork={run.visibility !== "private" || canControl} forkEnabled={forkEnabled} signedIn={viewer !== null} /></Tabs.Content>
          </div>
        </Tabs.Root>
      </div>

      <p className="mt-4 text-xs text-muted">
        {run.visibility} · {run.model_id ?? "model not recorded"} · {run.game ?? "game not recorded"}
        {run.parent_checkpoint_id ? " · forked from a checkpoint" : ""} · <Link href="/leaderboard">Leaderboard</Link>
      </p>
      {run.fork_patch ? <p className="mono mt-1 text-xs text-muted">fork changed: {JSON.stringify(run.fork_patch)}</p> : null}
    </div>
  );
}
