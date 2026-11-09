import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { NewRun } from "@/components/new-run";
import { getViewer } from "@/server/viewer";

export const metadata: Metadata = { title: "New run", robots: { index: false } };

export default async function Page() {
  if (!(await getViewer())) redirect("/auth/signin?next=/runs/new");
  return <NewRun />;
}
