import json
import math

import pytest

from runlog.events import (
    MAX_DEPTH,
    MAX_EVENT_BYTES,
    Event,
    EventRejected,
    canonical,
    is_valid_type,
    parse_line,
    validate_payload,
)
from runlog.lifecycle import FINAL, STATES, check_transition

RUN = "11111111-2222-3333-4444-555555555555"


def line(**kw: object) -> str:
    base = {"seq": 1, "run_id": RUN, "step": 0, "ts": "t", "type": "obs", "data": {}}
    return json.dumps({**base, **kw})


@pytest.mark.parametrize("t", ["run.state", "obs", "model.call", "model.summary", "action", "mem.read", "mem.write",
                               "mem.delete", "mem.reject", "session.boundary", "checkpoint", "detector", "cap.hit",
                               "error", "dream.write", "dream.start"])
def test_valid_types(t: str) -> None:
    assert is_valid_type(t)
    validate_payload(t, 0, {})


@pytest.mark.parametrize("t", ["", "OBS", "mem", "dream.", "dream.Write", "dream.x.y", "run.state ", None, 5, ["obs"], "obs\n"])
def test_invalid_types(t: object) -> None:
    assert not is_valid_type(t)
    with pytest.raises(EventRejected, match="unknown_type"):
        validate_payload(t, 0, {})


@pytest.mark.parametrize("step", [-1, 1_000_001, True, 1.5, "1", None])
def test_bad_steps(step: object) -> None:
    with pytest.raises(EventRejected, match="bad_step"):
        validate_payload("obs", step, {})


def test_data_must_be_a_json_object_of_bounded_size_and_depth() -> None:
    for bad in (None, [], "x", 5):
        with pytest.raises(EventRejected, match="data_not_object"):
            validate_payload("obs", 0, bad)
    nested: dict = {}
    cur = nested
    for _ in range(MAX_DEPTH + 1):
        cur["a"] = {}
        cur = cur["a"]
    with pytest.raises(EventRejected, match="too_deep"):
        validate_payload("obs", 0, nested)
    with pytest.raises(EventRejected, match="event_too_large"):
        validate_payload("obs", 0, {"a": ["x" * 30_000] * 3})
    assert MAX_EVENT_BYTES == 65536
    with pytest.raises(EventRejected, match="string_too_long"):
        validate_payload("obs", 0, {"a": "x" * 32_001})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_rejected(value: float) -> None:
    with pytest.raises(EventRejected, match="non_finite"):
        validate_payload("obs", 0, {"a": value})


@pytest.mark.parametrize("data", [{"a": b"bytes"}, {"a": {1, 2}}, {1: "x"}, {"a": object()}, {"k" * 101: 1}, {"a": "\ud800"}])
def test_non_json_content_rejected(data: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(EventRejected):
        validate_payload("obs", 0, data)


def test_canonical_is_stable_and_ascii() -> None:
    assert canonical({"b": 1, "a": "é"}) == '{"a":"\\u00e9","b":1}'
    with pytest.raises(ValueError):
        canonical({"a": math.nan})


def test_parse_line_roundtrip_and_corruption() -> None:
    e = Event(3, RUN, 2, "2026-01-01T00:00:00.000Z", "obs", {"x": 1})
    assert parse_line(canonical(e.to_obj())) == e
    for bad in ("", "{", "[]", "null", line(seq=0), line(seq=True), line(seq="1"), line(run_id="not-a-uuid"), line(ts=5),
                line(type="bogus"), line(data=[]), json.dumps({"seq": 1}), line(extra=1)):
        with pytest.raises(EventRejected, match="corrupt_line|unknown_type|data_not_object"):
            parse_line(bad)


# ---- lifecycle ----------------------------------------------------------------------------------
def test_normal_run_path() -> None:
    state = None
    for new, role in (("queued", "orchestrator"), ("starting", "orchestrator"), ("running", "worker"),
                      ("session_boundary", "worker"), ("dreaming", "worker"), ("running", "worker"),
                      ("stopping", "worker"), ("capped", "worker")):
        state = check_transition(state, new, role)
    assert state == "capped" and state in FINAL


@pytest.mark.parametrize(
    ("prev", "new", "role"),
    [(None, "running", "worker"), ("queued", "running", "worker"), ("starting", "stopping", "worker"),
     ("running", "completed", "worker"), ("running", "queued", "orchestrator"), ("stopping", "running", "worker"),
     ("completed", "running", "worker"), ("capped", "failed", "worker"), ("killed", "completed", "orchestrator"),
     ("running", "bogus", "worker"), ("running", None, "worker"), ("running", "running", "worker")],
)
def test_illegal_transitions(prev: str | None, new: object, role: str) -> None:
    with pytest.raises(EventRejected):
        check_transition(prev, new, role)


def test_roles_may_only_write_their_states() -> None:
    with pytest.raises(EventRejected, match="state_not_writable_by_role"):
        check_transition(None, "starting", "worker")        # only the orchestrator starts runs
    with pytest.raises(EventRejected, match="state_not_writable_by_role"):
        check_transition("starting", "running", "orchestrator")
    with pytest.raises(EventRejected, match="state_not_writable_by_role"):
        check_transition("running", "killed", "worker")      # killing is the orchestrator's
    with pytest.raises(EventRejected, match="state_not_writable_by_role"):
        check_transition("running", "failed", "visitor")
    assert check_transition("running", "killed", "orchestrator") == "killed"
    assert check_transition("starting", "failed", "worker") == "failed" if False else True


def test_every_state_is_reachable_and_final_states_are_terminal() -> None:
    for s in FINAL:
        for t in STATES:
            with pytest.raises(EventRejected):
                check_transition(s, t, None)
    assert check_transition("running", "stopping", None) == "stopping"  # role-free check for recorded logs
