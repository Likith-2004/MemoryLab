import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { AdminView } from "@/components/admin-view";
import { getViewer } from "@/server/viewer";

export const metadata: Metadata = { title: "Admin", robots: { index: false } };

export default async function Page() {
  const me = await getViewer();
  if (!me) redirect("/auth/signin?next=/admin");
  if (me.role !== "admin") notFound(); // convenience only: the API re-checks on every call
  return <AdminView />;
}
