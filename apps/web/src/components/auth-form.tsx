"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiProblem, api, describe, session } from "@/lib/client";
import { safeNext } from "@/lib/redirect";
import { Banner, Button, Field, inputClass } from "./ui";

export type AuthMode = "signin" | "signup" | "reset" | "magic";

const COPY: Record<AuthMode, { title: string; submit: string }> = {
  signin: { title: "Sign in", submit: "Sign in" },
  signup: { title: "Create an account", submit: "Create account" },
  reset: { title: "Reset your password", submit: "Send reset email" },
  magic: { title: "Sign in with an email link", submit: "Send link" },
};

/** Every email-sending path answers the same message whether or not the address has an account (the API guarantees it; we do not
 *  add anything that could differ). Sign-up, magic link and reset go to /api/v1/auth/*: never to GoTrue's own /signup, /otp or /recover. */
export function AuthForm({ mode, next }: { mode: AuthMode; next?: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const copy = COPY[mode];
  const needsPassword = mode === "signin" || mode === "signup";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "signin") {
        await session("login", { email, password });
        router.replace(safeNext(next));
        router.refresh();
      } else if (mode === "signup") {
        if (password.length < 12) throw new ApiProblem(422, "validation_failed", "Use at least 12 characters for the password.", null);
        const r = await api<{ message: string }>("/auth/signup", { method: "POST", body: { email, password } });
        setSent(r.message);
      } else {
        const r = await api<{ message: string }>(mode === "magic" ? "/auth/magic-link" : "/auth/recover", { method: "POST", body: { email } });
        setSent(r.message);
      }
    } catch (err) {
      setError(err instanceof ApiProblem && err.code === "validation_failed" && mode === "signup" ? err.title : describe(err));
    } finally {
      setPassword("");
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <section className="max-w-md">
        <h1 className="mb-3 text-xl font-semibold">Check your email</h1>
        <Banner tone="ok">{sent}</Banner>
        <p className="text-sm text-muted">
          {mode === "signup" ? "Open the confirmation link to finish creating the account, then " : "Then "}
          <Link href="/auth/signin">sign in</Link>.
        </p>
      </section>
    );
  }
  return (
    <section className="max-w-md">
      <h1 className="mb-3 text-xl font-semibold">{copy.title}</h1>
      {error ? <Banner tone="bad">{error}</Banner> : null}
      <form onSubmit={submit} noValidate={false}>
        <Field id="email" label="Email">
          <input id="email" name="email" type="email" required autoComplete="email" maxLength={254} className={inputClass}
            value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        {needsPassword ? (
          <Field id="password" label="Password" hint={mode === "signup" ? "At least 12 characters." : undefined}>
            <input id="password" name="password" type="password" required minLength={mode === "signup" ? 12 : 1} maxLength={128}
              autoComplete={mode === "signup" ? "new-password" : "current-password"} className={inputClass}
              value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
        ) : null}
        {/* CAPTCHA widget slot: sign-up and reset are protected by CAPTCHA in production (BLOCKED.md, needs site keys). */}
        <Button type="submit" disabled={busy}>{busy ? "Working…" : copy.submit}</Button>
      </form>
      <p className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {mode !== "signin" ? <Link href="/auth/signin">Sign in</Link> : null}
        {mode !== "signup" ? <Link href="/auth/signup">Create an account</Link> : null}
        {mode !== "magic" ? <Link href="/auth/magic">Email me a sign-in link</Link> : null}
        {mode !== "reset" ? <Link href="/auth/reset">Forgot password</Link> : null}
      </p>
    </section>
  );
}
