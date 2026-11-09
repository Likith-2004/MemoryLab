import type { ReactNode } from "react";
import { Banner } from "./ui";

/** Every legal page is a DRAFT for owner/legal review (docs/BLOCKED.md). The banner is part of the page, not a comment. */
export function LegalPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="max-w-3xl">
      <h1 className="mb-2 text-xl font-semibold">{title}</h1>
      <Banner tone="warn" role="status"><strong>Draft.</strong> This text describes how the lab works today. It has not been reviewed by a lawyer and is not final.</Banner>
      <div className="space-y-3 leading-relaxed [&_h2]:mt-5 [&_h2]:text-base [&_h2]:font-semibold [&_ul]:list-disc [&_ul]:pl-5">{children}</div>
    </div>
  );
}
