"""The plan's canary-key test: run with a fake key, then search every sink for it."""

import base64
import io
import json
import logging
import urllib.parse
import uuid
import zipfile
from compression import zstd
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from labharness import Limits, Session, load_bundle
from labharness.agent import AgentLoop, Caps, RunConfig
from labharness.memory import MemoryConfig
from labharness.presets import get_preset
from labharness.prompt import ObservationConfig
from labharness.providers import FakeProvider, ProviderError, ScriptedStep, SecretKey, Usage
from labharness.screenshots import ScreenshotPolicy
from runlog.client import HttpEventSink, RedactingSink
from runlog.manifest import build_manifest
from runlog.redaction import Redactor

from conftest import SECRET, make_env
from ingest.tokens import issue_token

FIXTURE = Path(__file__).resolve().parents[3] / "packages" / "games" / "fixture"
PATTERN_KEY = "sk-ant-api03-CANARY0canary1CANARY2canary3CANARY4canary5CANARY6canary7CANARY8xyz"   # matches a known pattern
ODD_KEY = "visitor-chosen-secret-value-12345"                                                         # matches no pattern


def forms(key: str) -> list[bytes]:
    raw = key.encode()
    return [raw, urllib.parse.quote(key, safe="").encode(), base64.b64encode(raw), base64.urlsafe_b64encode(raw),
            raw.hex().encode()]


def haystack(env, run_id: str, extra: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:  # type: ignore[no-untyped-def]
    """Every byte the system stored or emitted about the run."""
    out: list[tuple[str, bytes]] = list(extra)
    for path in sorted(p for p in env.store.root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        out.append((str(path.relative_to(env.store.root)), data))
        if path.name.endswith(".zst"):
            out.append((path.name + " (decompressed)", zstd.decompress(data)))
        if path.suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                out += [(f"{path.name}:{n}", z.read(n)) for n in z.namelist()]
    out.append(("repository", repr((env.repo.runs, env.repo.index, env.repo.memory_rows)).encode()))
    return out


def leaks(key: str, stuff: list[tuple[str, bytes]]) -> list[str]:
    return sorted({name for name, data in stuff for needle in forms(key) if needle in data})


def run_leaky_loop(tmp_path, caplog, *, key: str, worker_redacts: bool):  # type: ignore[no-untyped-def]
    caplog.set_level(logging.DEBUG)
    env = make_env(tmp_path)
    run_id = str(uuid.uuid4())
    env.repo.register(run_id)
    sent: list[tuple[str, bytes]] = []
    real_request = env.client.request

    def recording(method, url, **kw):  # type: ignore[no-untyped-def]
        if "json" in kw:
            sent.append((f"request {url}", json.dumps(kw["json"]).encode()))
        return real_request(method, url, **kw)

    env.client.request = recording  # type: ignore[method-assign]
    game = load_bundle(FIXTURE)
    config = RunConfig("fake", "fake-model", "general@1", MemoryConfig(),
                       observation=ObservationConfig(hide_extras=("input_frames", "frame_counter")),
                       screenshots=ScreenshotPolicy("never"), caps=Caps(max_steps=10))
    preset = get_preset("general@1")
    redactor = Redactor([key])
    worker_token = SecretKey(issue_token(SECRET, run_id, "worker", 600, env.now[0]))
    sink = HttpEventSink(env.client, run_id, worker_token, batch_size=4)
    # the orchestrator and the worker number their batches independently (both start at 1)
    assert env.post(run_id, 1, [{"type": "run.state", "step": 0, "data": {"state": "starting"}}], "orchestrator").status_code == 200
    script = [
        ScriptedStep((("press", {"buttons": ["Right"], "hold_frames": 1}),), f"I think the key is {key}", Usage(80, 20)),
        ScriptedStep((("mem_write", {"path": "notes/k.md", "content": f"remember {key}"}),), "", Usage(80, 20)),
        ScriptedStep(((f"tool_{key}", {"x": 1}), ("press", {"buttons": [key], "hold_frames": 1})), "", Usage(80, 20)),
        ProviderError(f"upstream rejected credential {key}"),
    ]
    with Session(game, Limits(), config.screenshots) as session:
        manifest = build_manifest(run_id=run_id, config=config, bundle=game, preset=preset, pyboy_version="2.9.0",
                                  max_total_frames=session.limits.max_total_frames)
        orch = {"authorization": f"Bearer {issue_token(SECRET, run_id, 'orchestrator', 600, env.now[0])}"}
        assert env.client.put(f"/v1/runs/{run_id}/manifest", json=manifest, headers=orch).status_code == 200
        out_sink = RedactingSink(sink, redactor) if worker_redacts else sink   # "forgot": no redaction at all
        loop = AgentLoop(session, FakeProvider(script), config, preset, out_sink, run_id=run_id,
                         content_filter=redactor.text if worker_redacts else None)
        result = loop.run()
    sink.flush()
    closed = env.client.post(f"/v1/runs/{run_id}/close", headers=orch)
    return env, run_id, result, closed, sent, redactor, caplog


def test_the_key_appears_nowhere_when_the_worker_redacts(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    env, run_id, result, closed, sent, redactor, caplog = run_leaky_loop(tmp_path, caplog, key=ODD_KEY, worker_redacts=True)
    assert result.state == "failed" and closed.status_code == 200
    stuff = haystack(env, run_id, [*sent, ("logs", caplog.text.encode()), ("result", repr(result).encode())])
    assert len(stuff) > 8                                                    # segments, manifest, bundle and its entries, repo...
    assert leaks(ODD_KEY, stuff) == []
    assert redactor.hits >= 5                                                # summary, memory, error x3, provider error
    assert ODD_KEY not in repr(env.repo.get(run_id))
    bundle = zipfile.ZipFile(io.BytesIO(env.store.get(f"runs/{run_id}/bundle.zip")))
    assert ODD_KEY not in bundle.read("events.jsonl").decode() and ODD_KEY not in bundle.read("memory/history.jsonl").decode()


def test_a_pattern_matching_key_is_also_caught_by_ingest_if_the_worker_forgets(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    env, run_id, result, closed, sent, _, caplog = run_leaky_loop(tmp_path, caplog, key=PATTERN_KEY, worker_redacts=False)
    assert closed.status_code == 200
    on_the_wire = leaks(PATTERN_KEY, sent)
    assert on_the_wire                                                       # the worker leaked it to ingest...
    assert leaks(PATTERN_KEY, haystack(env, run_id, [("logs", caplog.text.encode())])) == []   # ...but nothing was stored
    assert env.service.redactor.hits >= 1


def test_negative_control_an_unknown_unpatterned_key_that_the_worker_did_not_redact_is_stored(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Proves the search can fail: with no worker-side knowledge of a pattern-less key, ingest cannot
    know it, so it lands in the log. The worker-side Redactor is what protects such keys."""
    env, run_id, _, closed, sent, _, caplog = run_leaky_loop(tmp_path, caplog, key=ODD_KEY, worker_redacts=False)
    assert leaks(ODD_KEY, haystack(env, run_id, [])) != []
    assert closed.status_code == 200
    assert leaks(ODD_KEY, sent) != []


def test_logs_from_the_worker_and_ingest_loggers_are_scrubbed(caplog: pytest.LogCaptureFixture) -> None:
    from ingest.app import log
    caplog.set_level(logging.DEBUG)
    log.error("something with %s", PATTERN_KEY)
    assert PATTERN_KEY not in caplog.text and "[REDACTED]" in caplog.text


def test_httpx_debug_logging_never_shows_the_ingest_token(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    env = make_env(tmp_path)
    rid = env.run()
    token = env.token(rid)
    client = TestClient(env.client.app)
    client.post(f"/v1/runs/{rid}/events", json={"batch_no": 1, "events": [{"type": "obs", "step": 0, "data": {}}]},
                headers={"authorization": f"Bearer {token}"})
    assert token not in caplog.text
