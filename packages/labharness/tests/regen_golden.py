"""Regenerate tests/golden/fixture_route.json. Only run this deliberately: a changed golden
trace means the ROM, start state, harness semantics or PyBoy changed (bump HARNESS_VERSION
and record it in docs/CHANGELOG.md).   .venv/bin/python tests/regen_golden.py"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_cli import run

if __name__ == "__main__":
    out = Path(__file__).parent / "golden" / "fixture_route.json"
    out.write_text(json.dumps(run(), indent=1, sort_keys=True) + "\n")
    print("wrote", out)
