import Link from "next/link";
import type { LeaderRow } from "@/lib/types";
import { getViewer, serverGet } from "@/server/viewer";

export default async function Home() {
  const [me, top] = await Promise.all([getViewer(), serverGet<LeaderRow[]>("/leaderboard?limit=1")]);
  const featured = top?.[0];
  return (
    <main className="max-w-3xl">
      <h1 className="mb-2 text-2xl font-semibold">Does an agent&apos;s own written memory help it play?</h1>
      <p className="mb-4 text-lg">
        Memory Lab runs game-playing agents that differ in <strong>one</strong> memory variable, logs every step, and lets anyone replay a run and
        see exactly what the agent wrote down, when, and whether it was used.
      </p>
      <p className="mb-6 flex gap-3">
        <Link href={me ? "/runs/new" : "/auth/signup"} className="rounded border border-ink bg-ink px-4 py-2 text-surface no-underline">{me ? "Start a run" : "Create an account to start a run"}</Link>
        <Link href="/leaderboard" className="rounded border border-line bg-panel px-4 py-2 no-underline">Leaderboard</Link>
      </p>

      <section aria-labelledby="featured-h" className="mb-6 border border-line bg-panel p-3">
        <h2 id="featured-h" className="mb-1 text-base font-semibold">A real run to replay</h2>
        {featured ? (
          <p>The top verified run: <span className="mono">{featured.model_id ?? "a model"}</span> scored <span className="mono">{featured.progress_score}</span> in <span className="mono">{featured.steps}</span> steps.{" "}
            <Link href={`/runs/${featured.id}`}>Replay it step by step</Link>.</p>
        ) : (
          <p className="text-muted">No verified public run exists yet, so there is nothing to show here. We do not mock one.</p>
        )}
      </section>

      <section aria-labelledby="no-claim-h" className="mb-6">
        <h2 id="no-claim-h" className="mb-1 text-base font-semibold">What we do not claim</h2>
        <ul className="list-disc pl-5">
          <li>One run is not a finding. Comparisons show the number of runs, and warn below five.</li>
          <li>A higher score is not proof that memory caused it: the experiment names the single variable that changed, and nothing else.</li>
          <li>Results come from the games and models listed here, not from agents in general.</li>
          <li>Scores are computed by the lab from the game&apos;s memory, never reported by the agent.</li>
        </ul>
      </section>
      <p className="text-sm text-muted">Bring your own provider key: it is used for your run only and never stored.</p>
    </main>
  );
}
