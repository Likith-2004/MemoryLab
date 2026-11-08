import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { ExperimentsList } from "@/components/experiments-list";
import { getViewer } from "@/server/viewer";

export const metadata: Metadata = { title: "Experiments", robots: { index: false } };

export default async function Page() {
  if (!(await getViewer())) redirect("/auth/signin?next=/experiments");
  return <ExperimentsList />;
}
