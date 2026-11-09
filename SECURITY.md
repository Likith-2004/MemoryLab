# Security policy

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub: on the repository's **Security** tab choose **Report a vulnerability**
(<https://github.com/TheManishCode/MemoryLab/security/advisories/new>). That opens a private advisory only the maintainers can see, where we can
discuss it, prepare a fix and, if warranted, publish an advisory with credit to you.

Include what you can: the affected component and commit, how to reproduce it, the impact you believe it has, and whether it is already public.
Never include a real credential or a real person's data in a report; a description or a redacted sample is enough.

What to expect (this is a small project; these are intentions, not a contract): an acknowledgement within a few days, an assessment and a
plan shortly after, and a fix or a clear explanation. Please give us reasonable time before disclosing publicly.

## Scope

In scope: the code in this repository: the API, accounts, ingest, orchestrator, Docker filter, egress proxy, worker and web app, the harness,
the CLI, the database migrations and policies, the container and compose configuration, and the CI workflows.

Of particular interest: anything that exposes a visitor's provider API key; reads or changes another user's runs; escapes the sandbox or the
Docker filter; bypasses row-level security, the CSRF or origin checks, or the egress allow-list; or lets a workflow run untrusted code with
secrets or write access.

Out of scope: findings that need a compromised host or administrator, denial of service by sheer volume, missing hardening on a deployment we do
not operate (there is no public deployment yet), and reports from automated scanners without a demonstrated impact.

## Design and known limits

How the system is meant to resist attack, the evidence for each control and the limits we already know about are in
[`docs/SECURITY.md`](docs/SECURITY.md); launch status is in [`docs/LAUNCH.md`](docs/LAUNCH.md). Please read the known limits before reporting one.

## Supported versions

There is no released or deployed version yet. Fixes go to `main`.
