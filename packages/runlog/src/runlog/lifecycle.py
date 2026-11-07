"""Run lifecycle (plan: queued -> starting -> running -> stopping -> completed|capped|failed|killed).

Every transition is a `run.state` event; only the orchestrator and the worker may write them,
and each may write only some states.
"""

from .events import EventRejected

STATES = frozenset({"queued", "starting", "running", "session_boundary", "dreaming", "stopping",
                    "completed", "capped", "failed", "killed"})
FINAL = frozenset({"completed", "capped", "failed", "killed"})
_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"queued", "starting"}),
    "queued": frozenset({"starting", "failed", "killed"}),
    "starting": frozenset({"running", "failed", "killed"}),
    "running": frozenset({"session_boundary", "dreaming", "stopping", "failed", "killed"}),
    "session_boundary": frozenset({"running", "dreaming", "stopping", "failed", "killed"}),
    "dreaming": frozenset({"running", "session_boundary", "stopping", "failed", "killed"}),
    "stopping": frozenset({"completed", "capped", "failed", "killed"}),
}
WRITABLE_BY: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"queued", "starting", "failed", "killed"}),
    "worker": frozenset({"running", "session_boundary", "dreaming", "stopping", "completed", "capped", "failed"}),
}


def check_transition(prev: str | None, new: object, role: str | None) -> str:
    """Validate prev -> new. `role=None` checks only the state machine (used when verifying a
    recorded log, where the writer is not recorded)."""
    if not isinstance(new, str) or new not in STATES:
        raise EventRejected("bad_state")
    if role is not None and (role not in WRITABLE_BY or new not in WRITABLE_BY[role]):
        raise EventRejected("state_not_writable_by_role", f"{role} may not write {new}")
    if new not in _TRANSITIONS.get(prev, frozenset()):
        raise EventRejected("bad_transition", f"{prev} -> {new}")
    return new
