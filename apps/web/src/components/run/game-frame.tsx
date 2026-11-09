"use client";
import { useState } from "react";

/** The game picture: the nearest sampled frame at or before `step`, drawn pixel-crisp. The frame URL is our own /bff route, which
 *  redirects to a short-lived signed URL; a missing frame is a normal state, not an error. */
export function GameFrame({ runId, step }: { runId: string; step: number | null }) {
  const [failedStep, setFailedStep] = useState<number | null>(null);
  const missing = step === null || failedStep === step;
  return (
    <figure className="m-0 border border-line bg-panel p-2">
      <div className="aspect-[160/144] w-full bg-surface">
        {missing ? (
          <div className="flex h-full items-center justify-center p-3 text-center text-sm text-muted" role="status">
            {step === null ? "Waiting for the first frame" : "No frame was saved for this step"}
          </div>
        ) : (
          // eslint-disable-next-line @next/next/no-img-element -- a redirecting, signed, uncacheable URL: next/image cannot optimise it
          <img key={step} src={`/bff/api/runs/${runId}/frames/${step}`} alt={`Game screen at step ${step}`} width={160} height={144}
            className="pixel h-full w-full object-contain" onError={() => setFailedStep(step)} />
        )}
      </div>
      <figcaption className="mono mt-1 text-xs text-muted">step {step ?? "–"}</figcaption>
    </figure>
  );
}
