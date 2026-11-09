"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { session } from "@/lib/client";
import { Button } from "./ui";

export function SignOutButton() {
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  return (
    <Button variant="plain" disabled={busy} onClick={async () => {
      setBusy(true);
      try { await session("logout", {}); } finally { router.replace("/"); router.refresh(); setBusy(false); }
    }}>Sign out</Button>
  );
}
