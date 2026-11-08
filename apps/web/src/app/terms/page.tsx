import type { Metadata } from "next";
import { LegalPage } from "@/components/legal";

export const metadata: Metadata = { title: "Terms (draft)" };

export default function Page() {
  return (
    <LegalPage title="Terms of use">
      <h2>The service</h2>
      <p>Memory Lab runs game-playing agents against your own model-provider account so you can watch, replay, fork and compare them. It is a research tool and is provided as is, without warranty.</p>
      <h2>Your responsibilities</h2>
      <ul>
        <li>You pay your provider for the tokens your runs use. The lab enforces the caps you set but is not responsible for charges on your key.</li>
        <li>Do not try to break out of the sandbox, attack the service or other users, evade rate limits, or put content you have no right to share into public runs.</li>
        <li>Instructions and memory you write can be shown publicly if you make a run public.</li>
      </ul>
      <h2>Limits</h2>
      <p>One active run per account, a limited number of new runs per hour, and hard caps on steps, tokens and time. We may stop runs, hide content or suspend accounts that abuse the service.</p>
      <h2>Results</h2>
      <p>Scores are computed by the lab from the game&apos;s state. They describe the listed games and models only and are not a claim about agents in general.</p>
      <h2>Changes and contact</h2>
      <p>[Governing law, operator identity and contact details to be supplied by the owner.]</p>
    </LegalPage>
  );
}
