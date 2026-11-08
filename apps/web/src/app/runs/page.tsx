import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { MyRuns } from "@/components/my-runs";
import { getViewer } from "@/server/viewer";

export const metadata: Metadata = { title: "My runs", robots: { index: false } };

export default async function Page() {
  if (!(await getViewer())) redirect("/auth/signin?next=/runs");
  return <MyRuns />;
}
