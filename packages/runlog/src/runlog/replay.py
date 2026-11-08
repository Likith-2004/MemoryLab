"""Replay and verification from an event list: pure functions, no I/O.

`verify_log` checks structural integrity; `compute_score` and `memory_tree_at` rebuild derived
data; `verify_detectors` recomputes signals; `reexecute` re-runs the recorded game actions in
the emulator and compares every step's RAM digest (the deterministic check leaderboards need).
"""

from collections.abc import Sequence
from typing import Any

from labharness.bundle import GameBundle
from labharness.detectors import LoopEvent, detect
from labharness.emulator import Limits
from labharness.errors import HarnessError
from labharness.session import Session
from labharness.tools import action_from_call

from .events import MEM_DELETE_TYPES, MEM_WRITE_TYPES, Event, EventRejected
from .lifecycle import FINAL, check_transition


def verify_log(events: Sequence[Event]) -> list[str]:
    problems: list[str] = []
    if not events:
        return ["log is empty"]
    run_id = events[0].run_id
    state: str | None = None
    final = False
    last_ts = ""
    last_obs_step = -1
    versions: dict[str, int] = {}
    last_rev = 0
    for i, e in enumerate(events):
        if e.seq != i + 1:
            problems.append(f"seq {e.seq} at position {i + 1}")
        if e.run_id != run_id:
            problems.append(f"event {e.seq} belongs to another run")
        if e.ts < last_ts:
            problems.append(f"timestamp goes backwards at seq {e.seq}")
        last_ts = max(last_ts, e.ts)
        if final:
            problems.append(f"event {e.seq} after the run ended")
        if e.type == "run.state":
            try:
                state = check_transition(state, e.data.get("state"), None)
            except EventRejected as exc:
                problems.append(f"seq {e.seq}: {exc}")
                continue
            final = state in FINAL
        elif i == 0:
            problems.append("log does not start with run.state")
        if e.type == "obs":
            if e.step < last_obs_step:
                problems.append(f"obs step goes backwards at seq {e.seq}")
            last_obs_step = e.step
        if e.type in MEM_WRITE_TYPES | MEM_DELETE_TYPES:
            path, version, rev = e.data.get("path"), e.data.get("version"), e.data.get("rev")
            if not isinstance(path, str) or version != versions.get(path, 0) + 1:
                problems.append(f"memory version for {str(path)[:40]} not consecutive at seq {e.seq}")
            else:
                versions[path] = version
            if not isinstance(rev, int) or rev <= last_rev:
                problems.append(f"memory rev not increasing at seq {e.seq}")
            else:
                last_rev = rev
    return problems


def final_state(events: Sequence[Event]) -> tuple[str | None, str | None]:
    state, reason = None, None
    for e in events:
        if e.type == "run.state":
            state, reason = e.data.get("state"), e.data.get("reason")
    return state, reason


def compute_score(events: Sequence[Event]) -> dict[str, Any]:
    """Score from RAM-derived `obs` events: popcount of the OR of every milestone mask seen,
    tiebreak = step of the last milestone first reached."""
    seen = 0
    first_step: dict[int, int] = {}
    steps = tokens = memory_ops = dream_ops = 0
    for e in events:
        if e.type in ("mem.write", "mem.delete"):
            memory_ops += 1        # the agent's own changes; the dream job's are counted apart so a dreaming arm does not look like a writier agent
        elif e.type in ("dream.write", "dream.delete"):
            dream_ops += 1
        if e.type == "obs":
            mask = e.data["snapshot"]["milestones"]
            for bit in range(mask.bit_length()):
                if mask >> bit & 1 and not seen >> bit & 1:
                    first_step[bit] = e.step
            seen |= mask
            steps = max(steps, e.step)
        elif e.type in ("model.call", "dream.call"):
            tokens = e.data["tokens_total"]
    state, reason = final_state(events)
    return {"progress_score": seen.bit_count(), "milestones_mask": seen, "tiebreak_steps": max(first_step.values(), default=0),
            "first_step": {str(b): s for b, s in sorted(first_step.items())}, "steps": steps, "tokens": tokens,
            "memory_ops": memory_ops, "dream_ops": dream_ops, "final_state": state, "end_reason": reason}


def memory_rows(events: Sequence[Event]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for e in events:
        if e.type in MEM_WRITE_TYPES:
            d = e.data
            rows.append({"path": d["path"], "version": d["version"], "step": d["step"], "rev": d["rev"],
                         "content": d["content"], "chars": d["chars"], "deleted": False, "sha256": d["after_sha256"]})
        elif e.type in MEM_DELETE_TYPES:
            d = e.data
            rows.append({"path": d["path"], "version": d["version"], "step": d["step"], "rev": d["rev"],
                         "content": "", "chars": 0, "deleted": True, "sha256": None})
    return rows


def memory_tree_at(events: Sequence[Event], step: int | None = None) -> dict[str, dict[str, Any]]:
    tree: dict[str, dict[str, Any]] = {}
    for row in memory_rows(events):
        if step is not None and row["step"] > step:
            break
        if row["deleted"]:
            tree.pop(row["path"], None)
        else:
            tree[row["path"]] = {"content": row["content"], "chars": row["chars"], "version": row["version"]}
    return tree


def verify_detectors(events: Sequence[Event]) -> list[str]:
    """Recompute detector signals from the other events and compare with the recorded ones."""
    loop_events = [LoopEvent(e.type, e.step, e.data) for e in events]
    expected = [(s.step, s.data) for s in detect(loop_events)]
    recorded = [(e.step, e.data) for e in events if e.type == "detector"]
    return [] if expected == recorded else [f"recorded detector events differ from recomputation ({len(recorded)} vs {len(expected)})"]


def reexecute(events: Sequence[Event], game: GameBundle) -> dict[str, Any]:
    """Re-run every recorded game action on a fresh emulator and compare each step's RAM digest
    and score to the recorded `obs`. Works because the game is deterministic (game_seed null)."""
    by_step: dict[int, list[dict[str, Any]]] = {}
    for e in events:
        if e.type == "action" and e.data.get("ok") and isinstance(e.data.get("action"), dict):
            by_step.setdefault(e.step, []).append(e.data["action"])
    obs = [e for e in events if e.type == "obs"]
    mismatches: list[str] = []
    checked = 0
    with Session(game, Limits(max_steps=100_000, max_total_frames=10**9)) as session:
        first = session.open()
        if not obs or obs[0].data["ram_sha256"] != first.ram_sha256:
            mismatches.append("step 0 RAM differs from the recorded start")
        for e in obs[1:]:
            executed: list[dict[str, Any]] = []
            try:
                for act in by_step.get(e.step, []):
                    action = action_from_call(act["tool"], {k: v for k, v in act.items() if k != "tool"})
                    executed.append({**act, "frames": session.advance(action)})
                rec = session.finish_step(executed)
            except HarnessError as exc:
                mismatches.append(f"step {e.step}: {type(exc).__name__}")
                break
            checked += 1
            if rec.step != e.step:
                mismatches.append(f"step numbering diverged at {e.step}")
                break
            if rec.ram_sha256 != e.data["ram_sha256"]:
                mismatches.append(f"step {e.step}: RAM digest differs")
            if rec.score != e.data["score"]:
                mismatches.append(f"step {e.step}: score differs")
        final_score = session.tracker.score
    return {"steps_checked": checked, "mismatches": mismatches, "score": final_score, "ok": not mismatches}
