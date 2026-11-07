"""`python -m runlog verify bundle.zip [--game DIR]`: check an export bundle offline."""

import argparse
import json
import sys
from pathlib import Path

from labharness.bundle import load_bundle

from .bundle import verify_bundle
from .events import parse_line
from .replay import reexecute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runlog")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify an export bundle")
    v.add_argument("bundle")
    v.add_argument("--game", help="game bundle directory: also re-execute the run and compare every step")
    args = parser.parse_args(argv)
    report = verify_bundle(Path(args.bundle).read_bytes())
    out: dict[str, object] = {"ok": report.ok, "problems": report.problems, "summary": report.summary}
    if args.game and report.ok:
        import zipfile
        events = [parse_line(line) for line in zipfile.ZipFile(args.bundle).read("events.jsonl").decode().splitlines()]
        rex = reexecute(events, load_bundle(args.game))
        out["reexecution"] = rex
        report.ok = rex["ok"]
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.ok else 1
