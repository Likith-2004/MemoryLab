"""Command line: `labharness run` executes a scripted action list; `labharness calibrate`
measures a game's release-frame count."""

import argparse
import json
import sys
from pathlib import Path

from .actions import parse_action
from .bundle import load_bundle
from .calibration import measure_release_frames
from .constants import HARNESS_VERSION
from .emulator import Limits
from .errors import HarnessError
from .screenshots import ScreenshotPolicy
from .session import Session

MAX_SCRIPT_BYTES = 1024 * 1024


def _load_script(path: Path) -> list[object]:
    raw = path.read_bytes()
    if len(raw) > MAX_SCRIPT_BYTES:
        raise HarnessError("script file too large")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"script is not valid JSON: {exc.msg}") from exc
    if not isinstance(doc, list):
        raise HarnessError("script must be a JSON list of action objects")
    return doc


def _run(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.bundle)
    actions = [parse_action(a) for a in _load_script(Path(args.script))]  # validates before any emulation
    policy = ScreenshotPolicy(args.screenshots, args.k, args.frame_scale)
    frames_dir = Path(args.frames_dir) if args.frames_dir else None
    if frames_dir:
        frames_dir.mkdir(parents=True, exist_ok=True)
    with Session(bundle, Limits(max_steps=args.max_steps), policy) as session:
        records = [session.open()]
        for action in actions:
            records.append(session.execute(action))
        manifest, manifest_sha = session.manifest(), session.manifest_sha256()
        score, tiebreak = session.tracker.score, session.tracker.tiebreak_steps
    entries = tuple(r.trace_entry() for r in records)
    if frames_dir:
        for r in records:
            if r.frame_png:
                (frames_dir / f"{r.step:07d}.png").write_bytes(r.frame_png)
    from .session import Trace

    trace = Trace(manifest_sha, entries, score, tiebreak)
    summary = {
        "harness_version": HARNESS_VERSION,
        "trace_sha256": trace.sha256,
        "manifest_sha256": manifest_sha,
        "score": score,
        "tiebreak_steps": tiebreak,
        "steps": len(entries) - 1,
    }
    if args.out:
        Path(args.out).write_text(json.dumps({**summary, "manifest": manifest, "entries": list(entries)}, sort_keys=True))
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    cal = measure_release_frames(load_bundle(args.bundle))
    json.dump({"release_frames": cal.release_frames, "passes": {str(k): v for k, v in cal.passes.items()}}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labharness", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a JSON action script against a game bundle")
    run.add_argument("--bundle", required=True, help="game bundle directory (contains bundle.json)")
    run.add_argument("--script", required=True, help="JSON list of {tool: press|wait, ...} actions")
    run.add_argument("--out", help="write the full trace JSON here")
    run.add_argument("--frames-dir", help="write captured PNG frames here")
    run.add_argument("--screenshots", default="every_k_or_change", choices=["never", "every_k", "every_k_or_change"])
    run.add_argument("--k", type=int, default=5)
    run.add_argument("--frame-scale", type=int, default=2)
    run.add_argument("--max-steps", type=int, default=2000)
    run.set_defaults(func=_run)
    cal = sub.add_parser("calibrate", help="measure release_frames for a game bundle")
    cal.add_argument("--bundle", required=True)
    cal.set_defaults(func=_calibrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (HarnessError, ValueError, OSError) as exc:
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 2
