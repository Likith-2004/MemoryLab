import type { Metadata } from "next";
import { LegalPage } from "@/components/legal";
import type { Game } from "@/lib/types";
import { serverGet } from "@/server/viewer";

export const metadata: Metadata = { title: "Licences and attributions (draft)" };

const THIRD_PARTY: [string, string, string][] = [
  ["PyBoy", "Game Boy emulator (imported unmodified)", "LGPL-3.0"],
  ["Next.js, React", "this web app", "MIT"],
  ["Radix UI, shadcn/ui (source-copied components)", "dialogs and tabs", "MIT"],
  ["Tailwind CSS", "styling", "MIT"],
  ["FastAPI, Starlette, Pydantic", "the API", "MIT / BSD-3-Clause"],
  ["Supabase (GoTrue, PostgREST, Postgres)", "accounts and data", "Apache-2.0 / MIT / PostgreSQL"],
];

export default async function Page() {
  const games = (await serverGet<Game[]>("/games")) ?? [];
  return (
    <LegalPage title="Licences and attributions">
      <h2>Games</h2>
      {games.length === 0 ? <p>No public game is available yet.</p> : (
        <ul>{games.map((g) => <li key={g.id}><span className="mono">{g.name}</span>: {g.licence}</li>)}</ul>
      )}
      <h2>Software</h2>
      <ul>{THIRD_PARTY.map(([n, what, l]) => <li key={n}>{n}: {what} ({l})</li>)}</ul>
      <p>The complete list with versions is in <span className="mono">docs/THIRD_PARTY.md</span> in the source repository. [Final attribution text to be approved by the owner.]</p>
    </LegalPage>
  );
}
