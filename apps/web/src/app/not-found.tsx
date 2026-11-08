import Link from "next/link";

export default function NotFound() {
  return (
    <main id="main" className="mx-auto max-w-3xl p-6">
      <h1 className="text-2xl font-semibold">Not found</h1>
      <p className="mt-2 text-muted">There is nothing here, or you may not see it.</p>
      <p className="mt-4"><Link href="/">Back to the lab</Link></p>
    </main>
  );
}
