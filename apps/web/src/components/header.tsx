import Link from "next/link";
import { getViewer } from "@/server/viewer";
import { SignOutButton } from "./sign-out-button";

export async function Header() {
  const me = await getViewer();
  const link = "px-2 py-1 hover:underline";
  return (
    <header className="border-b border-line bg-panel">
      <nav aria-label="Main" className="mx-auto flex max-w-6xl flex-wrap items-center gap-1 px-4 py-2">
        <Link href="/" className="mr-3 font-semibold no-underline text-ink">Memory Lab</Link>
        <Link href="/leaderboard" className={link}>Leaderboard</Link>
        {me ? (
          <>
            <Link href="/runs/new" className={link}>New run</Link>
            <Link href="/runs" className={link}>My runs</Link>
            <Link href="/experiments" className={link}>Experiments</Link>
            {me.role === "admin" ? <Link href="/admin" className={link}>Admin</Link> : null}
            <span className="ml-auto flex items-center gap-2">
              <Link href="/settings" className={link}>Account</Link>
              <SignOutButton />
            </span>
          </>
        ) : (
          <span className="ml-auto flex items-center gap-2">
            <Link href="/auth/signin" className={link}>Sign in</Link>
            <Link href="/auth/signup" className="rounded border border-ink bg-ink px-3 py-1 text-surface no-underline">Sign up</Link>
          </span>
        )}
      </nav>
    </header>
  );
}
