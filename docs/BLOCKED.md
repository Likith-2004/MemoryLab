# Blocked items

Format: item, what is blocked, why (boundary), what is needed from the owner.

| ID | Item | Blocked by | Needed |
|---|---|---|---|
| B-3 | gVisor/Firecracker runtime | `runsc` not installable here (no sudo) | Install gVisor on the run-worker host and set `SandboxResources.runtime="runsc"`; re-run `test_docker.py`. |
| B-2 | S3-compatible object store backend and signed URLs | Local development only: no cloud accounts (boundary 1). `LocalObjectStore` implements the `ObjectStore` protocol | An S3/R2 bucket and credentials at deployment time; a MinIO-backed backend can be added and tested locally in M10/M11. |
| B-1 | Real Anthropic model calls and the metered pilot (3 runs per arm A and B) | No `.env` with a key (boundary 1: money) | Put `ANTHROPIC_API_KEY` in `.env` (gitignored) and a spend cap; until then every real-model check is "NOT RUN". The adapter's request/response shapes follow Anthropic's public docs and are tested only against a mock transport and recorded-style fixtures; `temperature`, prompt caching and `tool_choice: any` behaviour with the newest models are unverified. |
| B-4 | Provider key validation in the API (`KeyValidator`) | Needs a real key to test (boundary 1: money). The entrypoint wires a validator that rejects everything | Same `.env` key as B-1; then implement the one-cheap-call validator and test it with a capped key. |
| B-5 | CAPTCHA on signup/reset, gateway rules (block raw `/auth/v1/signup`, Origin checks), load/abuse tests | Needs a deployed gateway and CAPTCHA keys | Decide the gateway at hosting time (spec decision #2); then add the rules and the k6 run. |
| B-6 | Fork from a checkpoint, end to end | The worker cannot start a job from a checkpoint: `Session.restore` and `MemoryService.forked` exist, but the job spec, worker and orchestrator do not use them, and the orchestrator refuses any submit with `parent` (422). `POST /runs/{id}/fork` is therefore disabled (`MEMORYLAB_FORK_ENABLED`, default off) and tested only against a fake orchestrator: NOT RUN end to end | Build the worker-side resume (job spec `parent`, restore savestate and memory rows, transcript offset), then lift the orchestrator refusal and set `MEMORYLAB_FORK_ENABLED=1`. |
