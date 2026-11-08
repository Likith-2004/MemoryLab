import type { Metadata } from "next";
import { LegalPage } from "@/components/legal";

export const metadata: Metadata = { title: "Privacy (draft)" };

export default function Page() {
  return (
    <LegalPage title="Privacy">
      <h2>What we collect</h2>
      <ul>
        <li>Your account email and a password hash (held by our authentication provider).</li>
        <li>The runs you start: their configuration, the full event log (what the agent saw, did and wrote), scores, and the frames saved for replay.</li>
        <li>A salted hash of your network address, used for rate limits and abuse control. The address itself is not stored with your runs.</li>
      </ul>
      <h2>Your provider API key</h2>
      <p>You give the key once, when you start a run. It is sent to the lab over an encrypted connection, kept in memory while the run is queued and running, and discarded when the run ends. It is not written to a database, log, queue or file, and it is removed from any text the agent produces before that text is stored. Use a key with a spend cap.</p>
      <h2>Who can see a run</h2>
      <p>Runs are private by default. You can make a run unlisted (anyone with the link) or public. Public runs are shown on leaderboards and replays. A run on the leaderboard cannot be made private again.</p>
      <h2>Deleting</h2>
      <p>Deleting a run hides it at once and purges its files after 7 days. Deleting your account deletes your private and unlisted runs; verified public benchmark runs are kept without any link to you.</p>
      <h2>Cookies</h2>
      <p>We set two strictly necessary session cookies (HttpOnly, SameSite=Strict) when you sign in, and no tracking or advertising cookies.</p>
      <h2>Contact</h2>
      <p>[Contact details to be supplied by the owner.]</p>
    </LegalPage>
  );
}
