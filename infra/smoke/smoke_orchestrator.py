"""SMOKE ONLY (mounted by infra/compose.smoke.yaml, never part of an image): the production orchestrator with a scripted provider, so
the stack can play a run with no model key and no money. The sandbox, worker, ingest, proxy and network are the real ones; only the
model's answers come from a script. `allow_fake_provider` is switched on here and nowhere else."""

from dataclasses import replace
from typing import Any

import orchestrator.main as prod
from orchestrator.core import Orchestrator, Submission

STEPS: list[dict[str, Any]] = [
    {"calls": [["press", {"buttons": ["Right"], "hold_frames": 1}]] + ([["mem_write", {"path": "notes/a.md", "content": "door at 3,3\n"}]] if i == 0 else []),
     "text": f"step {i + 1}", "tokens": 100} for i in range(4)]


class Scripted(Orchestrator):
    def submit(self, sub: Submission) -> dict[str, Any]:
        script = STEPS[2:] if sub.parent else STEPS                   # a fork resumes at the checkpoint after step 2
        return super().submit(replace(sub, run_config={**sub.run_config, "provider": "fake"},
                                      provider={"name": "fake", "api_key": sub.provider.get("api_key"), "script": script}))


_config_from_env = prod.config_from_env
prod.config_from_env = lambda env=None: (replace(_config_from_env(env)[0], allow_fake_provider=True), _config_from_env(env)[1])  # type: ignore[assignment]
prod.Orchestrator = Scripted  # type: ignore[misc]
build = prod.build
