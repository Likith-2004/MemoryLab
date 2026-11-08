import Link from "next/link";

export default function NotFound() {
  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-semibold">Not found</h1>
      <p className="mt-2 text-muted">There is nothing here, or you may not see it.</p>
      <p className="mt-4"><Link href="/">Back to the lab</Link></p>
    </div>
  );
}
