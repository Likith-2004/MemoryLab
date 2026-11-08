"use client";
/* Adapted from shadcn/ui (MIT, https://github.com/shadcn-ui/ui) `dialog` over Radix UI Dialog (MIT, https://github.com/radix-ui/primitives):
 * restyled with our tokens (no blur, no gradient, no animation). Recorded in docs/THIRD_PARTY.md. */
import * as RadixDialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";

export const Dialog = RadixDialog.Root;
export const DialogTrigger = RadixDialog.Trigger;
export const DialogClose = RadixDialog.Close;

export function DialogContent({ title, description, children, side = "center" }: { title: string; description?: string; children: ReactNode; side?: "center" | "right" }) {
  const place = side === "right" ? "right-0 top-0 h-full w-full max-w-xl overflow-y-auto" : "left-1/2 top-1/2 max-h-[90vh] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto";
  return (
    <RadixDialog.Portal>
      <RadixDialog.Overlay className="fixed inset-0 bg-black/50" />
      <RadixDialog.Content className={`fixed ${place} border border-line bg-panel p-4`} aria-describedby={description ? undefined : undefined}>
        <div className="mb-3 flex items-start justify-between gap-4">
          <RadixDialog.Title className="text-lg font-semibold">{title}</RadixDialog.Title>
          <RadixDialog.Close className="rounded border border-line px-2 py-0.5 text-sm" aria-label="Close">Close</RadixDialog.Close>
        </div>
        {description ? <RadixDialog.Description className="mb-3 text-sm text-muted">{description}</RadixDialog.Description> : null}
        {children}
      </RadixDialog.Content>
    </RadixDialog.Portal>
  );
}
