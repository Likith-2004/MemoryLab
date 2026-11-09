import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ExperimentView } from "@/components/experiment-view";
import type { Experiment } from "@/lib/types";
import { getViewer, serverGet } from "@/server/viewer";

type Params = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const e = await serverGet<Experiment>(`/experiments/${encodeURIComponent((await params).id)}`);
  return { title: e?.title ?? "Not found", robots: e?.visibility === "public" ? undefined : { index: false, follow: false } };
}

export default async function Page({ params }: Params) {
  const { id } = await params;
  const [e, viewer] = await Promise.all([serverGet<Experiment>(`/experiments/${encodeURIComponent(id)}`), getViewer()]);
  if (!e) notFound();
  return <ExperimentView id={id} viewerId={viewer?.id ?? null} />;
}
