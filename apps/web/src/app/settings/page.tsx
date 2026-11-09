import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { SettingsView } from "@/components/settings-view";
import { getViewer } from "@/server/viewer";

export const metadata: Metadata = { title: "Account", robots: { index: false } };

export default async function Page() {
  const me = await getViewer();
  if (!me) redirect("/auth/signin?next=/settings");
  return <SettingsView me={me} />;
}
