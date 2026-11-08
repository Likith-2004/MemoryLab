import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { RunView } from "@/components/run/run-view";
import { webEnv, wsOrigin } from "@/lib/env";
import type { Run } from "@/lib/types";
import { getViewer, serverGet } from "@/server/viewer";

type Params = { params: Promise<{ id: string }>; searchParams: Promise<{ at?: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { id } = await params;
  const run = await serverGet<Run>(`/runs/${encodeURIComponent(id)}`);
  if (!run) return { title: "Not found", robots: { index: false } };
  const isPublic = run.visibility === "public";
  return {
    title: run.title ?? `Run ${run.id.slice(0, 8)}`,
    robots: isPublic ? undefined : { index: false, follow: false }, // private and unlisted runs are never indexed
    openGraph: isPublic ? { title: run.title ?? "A Memory Lab run", description: `${run.model_id ?? "An agent"} playing ${run.game ?? "a game"}`, type: "website" } : undefined,
  };
}

export default async function RunPage({ params, searchParams }: Params) {
  const [{ id }, { at }] = await Promise.all([params, searchParams]);
  const run = await serverGet<Run>(`/runs/${encodeURIComponent(id)}`);
  if (!run) notFound();
  const [viewer, features] = await Promise.all([getViewer(), serverGet<{ fork: boolean }>("/features")]);
  const parsed = at !== undefined && /^\d{1,7}$/.test(at) ? Number(at) : null;
  return <RunView run={run} viewer={viewer ? { id: viewer.id, role: viewer.role } : null} wsBase={wsOrigin(webEnv().publicApiUrl)}
    forkEnabled={features?.fork ?? false} initialAt={parsed} />;
}
