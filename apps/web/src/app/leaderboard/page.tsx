import type { Metadata } from "next";
import Link from "next/link";
import { PageTitle } from "@/components/ui";
import { groupByModel } from "@/lib/stats";
import type { LeaderRow } from "@/lib/types";
import { serverGet } from "@/server/viewer";

export const metadata: Metadata = { title: "Leaderboard", description: "Verified benchmark runs: median, range and number of runs per model." };

export default async function Page({ searchParams }: { searchParams: Promise<{ model?: string }> }) {
  const { model } = await searchParams;
  const rows = (await serverGet<LeaderRow[]>("/leaderboard?limit=100")) ?? [];
  const groups = groupByModel(rows);
  const shown = model ? rows.filter((r) => (r.model_id ?? "unknown model") === model) : rows;
  return (
    <div>
      <PageTitle sub="Only verified benchmark runs. Unverified and demo runs are never ranked.">Leaderboard</PageTitle>
      {rows.length === 0 ? (
        <p className="border border-line bg-panel p-4">No verified runs yet. A run appears here only after it finishes in Benchmark mode and is verified.</p>
      ) : (
        <>
          <h2 className="mb-1 text-base font-semibold">By model</h2>
          <div className="mb-5 overflow-x-auto">
            <table className="w-full border border-line bg-panel text-left text-sm">
              <caption className="sr-only">Median, range and number of verified runs per model</caption>
              <thead><tr className="border-b border-line"><th scope="col" className="p-2">Model</th><th scope="col" className="p-2">Median</th><th scope="col" className="p-2">Range</th><th scope="col" className="p-2">Runs (n)</th></tr></thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={g.model} className="border-b border-line">
                    <th scope="row" className="mono p-2 font-normal"><Link href={`/leaderboard?model=${encodeURIComponent(g.model)}`}>{g.model}</Link></th>
                    <td className="mono p-2">{g.median}</td><td className="mono p-2">{g.min}–{g.max}</td>
                    <td className="mono p-2">{g.n}{g.n < 5 ? " (few runs)" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h2 className="mb-1 text-base font-semibold">Runs{model ? <> for <span className="mono">{model}</span> · <Link href="/leaderboard">all models</Link></> : ""}</h2>
          <div className="overflow-x-auto">
            <table className="w-full border border-line bg-panel text-left text-sm">
              <caption className="sr-only">Verified runs ranked by score</caption>
              <thead><tr className="border-b border-line"><th scope="col" className="p-2">#</th><th scope="col" className="p-2">Run</th><th scope="col" className="p-2">Model</th><th scope="col" className="p-2">Score</th><th scope="col" className="p-2">Steps</th></tr></thead>
              <tbody>
                {shown.map((r, i) => (
                  <tr key={r.id} className="border-b border-line">
                    <td className="mono p-2">{i + 1}</td>
                    <td className="p-2"><Link href={`/runs/${r.id}`} className="mono">{r.id.slice(0, 8)}</Link> <span className="text-muted">replay</span></td>
                    <td className="mono p-2">{r.model_id ?? "unknown"}</td><td className="mono p-2">{r.progress_score}</td><td className="mono p-2">{r.steps}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
