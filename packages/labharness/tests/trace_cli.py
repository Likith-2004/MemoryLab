"""Run the fixture route in a fresh interpreter and print its trace as JSON (used by the
cross-process determinism test and by regen_golden.py)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I mode does not add the script dir

from helpers import FIXTURE_DIR, route
from labharness import Session, load_bundle


def run() -> dict:  # type: ignore[type-arg]
    with Session(load_bundle(FIXTURE_DIR)) as session:
        trace = session.run_script(route())
        return {
            "trace_sha256": trace.sha256,
            "manifest_sha256": trace.manifest_sha256,
            "score": trace.score,
            "tiebreak_steps": trace.tiebreak_steps,
            "steps": len(trace.entries) - 1,
            "ram_sha256_per_step": [e["ram_sha256"] for e in trace.entries],
            "final_snapshot": trace.entries[-1]["snapshot"],
        }


if __name__ == "__main__":
    json.dump(run(), sys.stdout, sort_keys=True)
