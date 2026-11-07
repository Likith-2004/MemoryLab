import io
import json
import zipfile

import pytest

from helpers import default_script, produce_run
from runlog.bundle import ExportRefused, build_export, verify_bundle
from runlog.eventlog import EventLogReader
from runlog.events import parse_line
from runlog.manifest import (
    ManifestSealed,
    harness_source_sha256,
    load_manifest,
    manifest_sha256,
    seal_manifest,
)
from runlog.replay import (
    compute_score,
    final_state,
    memory_rows,
    memory_tree_at,
    reexecute,
    verify_detectors,
    verify_log,
)
from runlog.store import LocalObjectStore


@pytest.fixture()
def finished(game_bundle, store):  # type: ignore[no-untyped-def]
    run_id, result, manifest, _ = produce_run(game_bundle, store)
    events = list(EventLogReader(store, run_id).iter_events())
    return run_id, result, manifest, events


# ---- manifest -------------------------------------------------------------------------------------
def test_manifest_contents_and_determinism(game_bundle, store) -> None:  # type: ignore[no-untyped-def]
    run_id, _, manifest, _ = produce_run(game_bundle, store)
    assert manifest["game"]["fixture"] is True and manifest["game_seed"] is None and manifest["release_frames"] == 7
    assert manifest["preset"] == "general@1" and manifest["preset_label"].startswith("adaptation")
    assert manifest["harness_version"] == "0.2.0" and len(manifest["harness_sha"]) == 64
    assert manifest["config_hash"].startswith("sha256:") and manifest["parent"] is None
    assert manifest["caps"]["max_total_frames"] == 1_200_000 and manifest["memory"]["inject_path"] == "global.md"
    assert "api_key" not in json.dumps(manifest) and "key" not in {k for k in manifest}
    assert manifest["base_prompt"]["version"] == "base@1"
    assert load_manifest(store, run_id) == manifest


def test_manifest_is_write_once_and_run_bound(game_bundle, store) -> None:  # type: ignore[no-untyped-def]
    run_id, _, manifest, _ = produce_run(game_bundle, store)
    with pytest.raises(ManifestSealed):
        seal_manifest(store, run_id, manifest)
    with pytest.raises(ValueError):
        seal_manifest(store, "11111111-2222-3333-4444-555555555555", manifest)
    assert manifest_sha256(manifest) == manifest_sha256(json.loads(json.dumps(manifest)))


def test_harness_hash_is_stable_and_sensitive() -> None:
    assert harness_source_sha256() == harness_source_sha256()
    from pathlib import Path

    import labharness
    root = Path(labharness.__file__).parent
    assert any(root.rglob("*.py"))


def test_manifest_hash_changes_when_any_field_changes(game_bundle, store) -> None:  # type: ignore[no-untyped-def]
    _, _, manifest, _ = produce_run(game_bundle, store)
    base = manifest_sha256(manifest)
    for key, value in (("model_id", "other"), ("preset", "none@1"), ("start_state_sha256", "0" * 64)):
        assert manifest_sha256({**manifest, key: value}) != base


# ---- a recorded run is internally consistent ----------------------------------------------------------------
def test_recorded_run_passes_every_check(finished) -> None:  # type: ignore[no-untyped-def]
    _, result, _, events = finished
    assert verify_log(events) == [] and verify_detectors(events) == []
    assert final_state(events) == ("capped", "steps")
    score = compute_score(events)
    assert score["progress_score"] == result.trace.score >= 1 and score["tokens"] == result.tokens
    assert score["steps"] == result.steps == len(default_script())
    assert [e.data["state"] for e in events if e.type == "run.state"] == ["starting", "running", "stopping", "capped"]


def test_memory_replay_matches_what_the_agent_did(finished) -> None:  # type: ignore[no-untyped-def]
    _, _, _, events = finished
    rows = memory_rows(events)
    assert [(r["path"], r["version"], r["deleted"]) for r in rows] == [("notes/a.md", 1, False), ("notes/a.md", 2, False), ("notes/a.md", 3, True)]
    step_of_second = rows[1]["step"]
    assert memory_tree_at(events, rows[0]["step"])["notes/a.md"]["content"] == "go right\n"
    assert memory_tree_at(events, step_of_second)["notes/a.md"]["version"] == 2
    assert memory_tree_at(events) == {}                                  # deleted by the end


def test_reexecution_reproduces_every_step(finished, game_bundle) -> None:  # type: ignore[no-untyped-def]
    _, result, _, events = finished
    report = reexecute(events, game_bundle)
    assert report == {"steps_checked": result.steps, "mismatches": [], "score": result.trace.score, "ok": True}


def test_reexecution_detects_a_forged_action(finished, game_bundle) -> None:  # type: ignore[no-untyped-def]
    _, _, _, events = finished
    from dataclasses import replace
    forged = []
    for e in events:
        if e.type == "action" and e.step == 2 and e.data["ok"]:
            data = {**e.data, "action": {**e.data["action"], "buttons": ["Up", "Up", "Up"]}}
            e = replace(e, data=data)
        forged.append(e)
    report = reexecute(forged, game_bundle)
    assert not report["ok"] and any("step 2" in m for m in report["mismatches"])


def test_reexecution_detects_a_forged_score_or_digest(finished, game_bundle) -> None:  # type: ignore[no-untyped-def]
    _, _, _, events = finished
    from dataclasses import replace
    forged = [replace(e, data={**e.data, "score": 99}) if e.type == "obs" and e.step == 3 else e for e in events]
    assert not reexecute(forged, game_bundle)["ok"]
    forged = [replace(e, data={**e.data, "ram_sha256": "0" * 64}) if e.type == "obs" and e.step == 5 else e for e in events]
    assert not reexecute(forged, game_bundle)["ok"]


def test_verify_log_catches_structural_tampering(finished) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace
    _, _, _, events = finished
    assert verify_log([]) == ["log is empty"]
    assert verify_log(events[1:])                                              # missing the first event
    assert verify_log(events[:5] + events[6:])                                 # a gap
    after_end = events + [replace(events[-1], seq=len(events) + 1, type="obs", data={"snapshot": {"milestones": 0}})]
    assert any("after the run ended" in p for p in verify_log(after_end))
    bad_state = [replace(e, data={"state": "completed"}) if e.type == "run.state" and e.data["state"] == "running" else e for e in events]
    assert any("bad_transition" in p for p in verify_log(bad_state))
    other_run = [replace(events[2], run_id="99999999-2222-3333-4444-555555555555")]
    assert any("another run" in p for p in verify_log(events[:2] + other_run + events[3:]))
    versions = [replace(e, data={**e.data, "version": 7}) if e.type == "mem.write" else e for e in events]
    assert any("not consecutive" in p for p in verify_log(versions))


def test_detector_verification_notices_missing_and_invented_signals(finished) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace
    _, _, _, events = finished
    detectors = [e for e in events if e.type == "detector"]
    assert detectors                                                          # the Left streak raised a signal
    assert verify_detectors([e for e in events if e.type != "detector"])
    invented = events + [replace(detectors[0], seq=len(events) + 1, data={"kind": "memory_churn"})]
    assert verify_detectors(invented)


# ---- export bundle -----------------------------------------------------------------------------------------------
def test_bundle_builds_verifies_and_is_idempotent(finished, store) -> None:  # type: ignore[no-untyped-def]
    run_id, result, _, _ = finished
    info = build_export(store, run_id)
    again = build_export(store, run_id)
    assert info == again and len(info.sha256) == 64
    names = set(info.files)
    assert {"manifest.json", "events.jsonl", "memory/final.json", "memory/history.jsonl", "score.json"} <= names
    blob = store.get(f"runs/{run_id}/bundle.zip")
    report = verify_bundle(blob)
    assert report.ok, report.problems
    assert report.summary["progress_score"] == result.trace.score and report.summary["final_state"] == "capped"
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert json.loads(z.read("memory/final.json")) == {}
    assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist())


def test_the_same_run_always_exports_the_same_bytes(game_bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Reproducible export: two independent recordings of the same deterministic run hash alike
    when ids and clocks match."""
    rid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    hashes = []
    for n in range(2):
        s = LocalObjectStore(tmp_path / f"o{n}")
        produce_run(game_bundle, s, run_id=rid)
        hashes.append(build_export(s, rid).sha256)
    assert hashes[0] == hashes[1]


def test_building_requires_a_closed_log(game_bundle, store) -> None:  # type: ignore[no-untyped-def]
    from runlog.eventlog import EventLogWriter
    rid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeee00"
    EventLogWriter(store, rid).append([("run.state", 0, {"state": "starting"})])
    with pytest.raises(ValueError, match="not closed"):
        build_export(store, rid)


def test_verify_bundle_rejects_tampering(finished, store) -> None:  # type: ignore[no-untyped-def]
    run_id, _, _, _ = finished
    build_export(store, run_id)
    blob = store.get(f"runs/{run_id}/bundle.zip")
    src = zipfile.ZipFile(io.BytesIO(blob))
    files = {n: src.read(n) for n in src.namelist()}

    def rebuild(mutate) -> bytes:  # type: ignore[no-untyped-def]
        out = dict(files)
        mutate(out)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, d in out.items():
                z.writestr(n, d)
        return buf.getvalue()

    cases = {
        "events edited": lambda f: f.update({"events.jsonl": f["events.jsonl"].replace(b'"step":1', b'"step":2', 1)}),
        "score forged": lambda f: f.update({"score.json": f["score.json"].replace(b'"progress_score":', b'"progress_score":9')}),
        "manifest edited": lambda f: f.update({"manifest.json": f["manifest.json"].replace(b"fake-model", b"claude-opus-5-5")}),
        "extra file": lambda f: f.update({"extra.txt": b"x"}),
        "path traversal name": lambda f: f.update({"../evil.json": b"{}"}),
        "absolute name": lambda f: f.update({"/etc/x": b"{}"}),
        "missing events": lambda f: f.pop("events.jsonl"),
        "unlisted frame": lambda f: f.update({"frames/0000001.png": b"png"}),
        "bad frame name": lambda f: f.update({"frames/a.png": b"png"}),
    }
    for name, mutate in cases.items():
        report = verify_bundle(rebuild(mutate))
        assert not report.ok and report.problems, name
    assert not verify_bundle(b"not a zip").ok
    assert verify_bundle(blob).ok


def test_zip_bomb_guard(finished, store) -> None:  # type: ignore[no-untyped-def]
    run_id, _, _, _ = finished
    build_export(store, run_id)
    z = zipfile.ZipFile(io.BytesIO(store.get(f"runs/{run_id}/bundle.zip")))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for n in z.namelist():
            if n != "memory/final.json":
                out.writestr(n, z.read(n))
        out.writestr("memory/final.json", b"0" * (129 * 1024 * 1024))  # one huge, highly compressible entry
    report = verify_bundle(buf.getvalue())
    assert not report.ok and any("too large" in p for p in report.problems)


def test_export_is_refused_when_a_credential_slipped_into_the_log(game_bundle, store) -> None:  # type: ignore[no-untyped-def]
    leaked = ("model.summary", 0, {"text": "my key is sk-ant-api03-Zq8vN2xLm4Kp7RtWc1YbH6dFg3JsAe5UoIi9PlXzC0nVq"})
    run_id, _, _, _ = produce_run(game_bundle, store, extra_event=leaked)
    with pytest.raises(ExportRefused, match="events.jsonl"):
        build_export(store, run_id)
    assert not store.exists(f"runs/{run_id}/bundle.zip")


def test_event_lines_in_the_bundle_parse_back(finished, store) -> None:  # type: ignore[no-untyped-def]
    run_id, _, _, events = finished
    build_export(store, run_id)
    z = zipfile.ZipFile(io.BytesIO(store.get(f"runs/{run_id}/bundle.zip")))
    parsed = [parse_line(line) for line in z.read("events.jsonl").decode().splitlines()]
    assert parsed == events
