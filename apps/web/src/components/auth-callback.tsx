"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { session } from "@/lib/client";
import { Banner } from "./ui";

/** The email link lands here with the session in the URL fragment. It is posted once to our server (which turns it into httpOnly
 *  cookies) and removed from the address bar and history straight away. */
export function AuthCallback() {
  const router = useRouter();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const access = params.get("access_token");
    const refresh = params.get("refresh_token");
    window.history.replaceState(null, "", window.location.pathname);
    void (async () => {
      try {
        if (!access || !refresh) throw new Error("no session in the link");
        await session("callback", { access_token: access, refresh_token: refresh, expires_in: Number(params.get("expires_in")) || 900 });
        router.replace("/runs");
        router.refresh();
      } catch {
        setFailed(true);
      }
    })();
  }, [router]);
  return failed
    ? <section className="max-w-md"><Banner tone="bad">That sign-in link is not valid any more.</Banner><Link href="/auth/magic">Get a new link</Link></section>
    : <p role="status">Signing you in…</p>;
}
