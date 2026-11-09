import type { Metadata } from "next";
import { AuthCallback } from "@/components/auth-callback";

export const metadata: Metadata = { title: "Signing in", robots: { index: false } };

export default function Page() {
  return <AuthCallback />;
}
