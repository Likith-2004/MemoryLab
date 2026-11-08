"""Frames (302 to a 60 s signed URL) and export (verified, re-scanned bundle + sha256)."""

import hashlib
import io
import logging
import uuid
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import Env, bearer
from test_authz_matrix import hdr, seed

PNG = b"\x89PNG\r\n\x1a\n" + b"frame-bytes-"


def put_frames(env: Env, run: str, steps: list[int]) -> None:
    for s in steps:
        env.store.put(f"runs/{run}/frames/{s:07d}.png", PNG + str(s).encode())


def make_bundle(files: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in (files or {"bundle.json": b"{}", "events.jsonl": b'{"seq":1}\n'}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def seal(env: Env, run: str, blob: bytes | None = None, *, sha: str | None = None, status: str = "completed",
         store: bool = True) -> bytes:
    blob = blob if blob is not None else make_bundle()
    if store:
        env.store.put(f"runs/{run}/bundle.zip", blob, overwrite=True)
    env.admin.execute("update public.runs set status = %s where id = %s", (status, run))
    env.admin.execute("insert into public.run_ingest_state (run_id, bundle_sha256) values (%s, %s) "
                      "on conflict (run_id) do update set bundle_sha256 = excluded.bundle_sha256",
                      (run, sha or hashlib.sha256(blob).hexdigest()))
    return blob


def local_path(url: str) -> str:
    u = urlparse(url)
    return f"{u.path}?{u.query}"


# ---------------------------------------------------------------- frames

def test_a_frame_request_redirects_to_the_nearest_sampled_frame_at_or_before_the_step(env: Env) -> None:
    ids = seed(env, "public")
    put_frames(env, ids["run"], [0, 10, 20])
    for step, expected in ((0, 0), (9, 0), (10, 10), (15, 10), (19, 10), (20, 20), (999, 20)):
        r = env.client.get(f"/api/v1/runs/{ids['run']}/frames/{step}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["x-frame-step"] == str(expected), (step, r.headers)
        assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
        assert r.content == b""


def test_no_frame_at_or_before_the_step_is_a_404(env: Env) -> None:
    ids = seed(env, "public")
    assert env.client.get(f"/api/v1/runs/{ids['run']}/frames/5").status_code == 404          # no frames at all
    put_frames(env, ids["run"], [10, 20])
    r = env.client.get(f"/api/v1/runs/{ids['run']}/frames/9")
    assert r.status_code == 404 and r.json()["code"] == "not_found"                         # earliest is 10 > 9
    assert env.client.get(f"/api/v1/runs/{ids['run']}/frames/10", follow_redirects=False).status_code == 302


@pytest.mark.parametrize("step", ["-1", "1000001", "abc", "1.5", "%00"])
def test_a_bad_step_is_a_422(env: Env, step: str) -> None:
    ids = seed(env, "public")
    put_frames(env, ids["run"], [0])
    assert env.client.get(f"/api/v1/runs/{ids['run']}/frames/{step}").status_code in (404, 422)
    assert env.client.get(f"/api/v1/runs/{ids['run']}/frames/{step}", follow_redirects=False).status_code != 302


@pytest.mark.parametrize("visibility", ["private", "unlisted", "public"])
@pytest.mark.parametrize("who", ["anon", "owner", "other", "admin"])
def test_frames_follow_run_visibility_with_the_same_404(env: Env, who: str, visibility: str) -> None:
    ids = seed(env, visibility)
    put_frames(env, ids["run"], [0])
    r = env.client.get(f"/api/v1/runs/{ids['run']}/frames/3", headers=hdr(ids, who), follow_redirects=False)
    if visibility != "private" or who in ("owner", "admin"):
        assert r.status_code == 302
    else:
        missing = env.client.get(f"/api/v1/runs/{uuid.uuid4()}/frames/3", headers=hdr(ids, who))
        assert (r.status_code, r.json()) == (missing.status_code, missing.json()) == (404, missing.json())


def test_a_deleted_runs_frames_are_gone(env: Env) -> None:
    ids = seed(env, "public")
    put_frames(env, ids["run"], [0])
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (ids["run"],))
    assert env.client.get(f"/api/v1/runs/{ids['run']}/frames/3").status_code == 404


def test_another_runs_frames_are_never_served(env: Env) -> None:
    a, b = seed(env, "public"), seed(env, "private")
    put_frames(env, b["run"], [0, 5])
    assert env.client.get(f"/api/v1/runs/{a['run']}/frames/9").status_code == 404           # a has none; b's are not a's


def test_the_signed_url_serves_exactly_that_frame_for_sixty_seconds(env: Env) -> None:
    ids = seed(env, "public")
    put_frames(env, ids["run"], [0, 10])
    r = env.client.get(f"/api/v1/runs/{ids['run']}/frames/12", follow_redirects=False)
    url = r.headers["location"]
    q = parse_qs(urlparse(url).query)
    assert int(q["exp"][0]) == int(env.clock[0]) + 60                                       # a 60 s window
    got = env.client.get(local_path(url))
    assert got.status_code == 200 and got.content == PNG + b"10" and got.headers["content-type"] == "image/png"
    assert got.headers["cache-control"] == "no-store" and got.headers["x-content-type-options"] == "nosniff"
    env.clock[0] += 59
    assert env.client.get(local_path(url)).status_code == 200
    env.clock[0] += 2                                                                       # past the 60 s
    assert env.client.get(local_path(url)).status_code == 404


def test_a_tampered_signed_url_is_refused(env: Env) -> None:
    ids = seed(env, "public")
    other = seed(env, "private")
    put_frames(env, ids["run"], [0])
    put_frames(env, other["run"], [0])
    url = env.client.get(f"/api/v1/runs/{ids['run']}/frames/0", follow_redirects=False).headers["location"]
    u = urlparse(url)
    q = parse_qs(u.query)
    token, exp, sig = u.path.rsplit("/", 1)[1], q["exp"][0], q["sig"][0]
    forged_key = env.signer.sign_get(f"runs/{other['run']}/frames/0000000.png", 60)
    other_token = urlparse(forged_key).path.rsplit("/", 1)[1]
    for path in (f"/_blob/{token}?exp={int(exp) + 3600}&sig={sig}",             # longer expiry, same signature
                 f"/_blob/{other_token}?exp={exp}&sig={sig}",                   # another object, same signature
                 f"/_blob/{token}?exp={exp}&sig={'0' * 64}", f"/_blob/{token}?exp={exp}", f"/_blob/{token}",
                 f"/_blob/{token}?exp=abc&sig={sig}", "/_blob/!!!?exp=1&sig=2",
                 f"/_blob/{token}?exp={exp}&sig={sig[:-1]}"):
        assert env.client.get(path).status_code == 404, path
    assert env.client.get(f"/_blob/{token}?exp={exp}&sig={sig}").status_code == 200       # the untouched one still works


def test_the_signer_refuses_keys_outside_the_store_and_short_secrets() -> None:
    from runlog.store import BadKey

    from api.signing import LocalUrlSigner
    s = LocalUrlSigner("http://x", b"k" * 32)
    for bad in ("../etc/passwd", "/abs", "runs//x", "A/b", "x" * 400):
        with pytest.raises(BadKey):
            s.sign_get(bad, 60)
    with pytest.raises(ValueError):
        LocalUrlSigner("http://x", b"short")


def test_a_signed_url_for_a_non_media_object_is_not_served(env: Env) -> None:
    ids = seed(env, "public")
    env.store.put(f"runs/{ids['run']}/events/segment.jsonl", b"secret events")
    url = env.signer.sign_get(f"runs/{ids['run']}/events/segment.jsonl", 60)
    assert env.client.get(local_path(url)).status_code == 404


def test_the_signed_url_never_appears_in_logs_or_bodies(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    ids = seed(env, "public")
    put_frames(env, ids["run"], [0])
    seal(env, ids["run"])
    caplog.set_level(logging.DEBUG)
    f = env.client.get(f"/api/v1/runs/{ids['run']}/frames/0", follow_redirects=False)
    e = env.client.get(f"/api/v1/runs/{ids['run']}/export")
    frame_url, export_url = f.headers["location"], e.json()["url"]
    sig1, sig2 = parse_qs(urlparse(frame_url).query)["sig"][0], parse_qs(urlparse(export_url).query)["sig"][0]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert sig1 not in logged and sig2 not in logged and "_blob" not in logged
    assert sig1 not in f.text and "location" not in {k.lower() for k in e.headers}


def test_frame_requests_are_read_limited(make_env: Any) -> None:
    env: Env = make_env(read_limit_per_min=2)
    ids = seed(env, "public")
    put_frames(env, ids["run"], [0])
    codes = [env.client.get(f"/api/v1/runs/{ids['run']}/frames/0", follow_redirects=False).status_code for _ in range(3)]
    assert codes == [302, 302, 429]


# ---------------------------------------------------------------- export

def test_export_returns_a_signed_url_for_the_sealed_bundle_and_its_sha256(env: Env) -> None:
    ids = seed(env, "public")
    blob = seal(env, ids["run"])
    r = env.client.get(f"/api/v1/runs/{ids['run']}/export")
    assert r.status_code == 200
    body = r.json()
    assert body["sha256"] == hashlib.sha256(blob).hexdigest() and body["bytes"] == len(blob) and body["expires_in"] == 60
    got = env.client.get(local_path(body["url"]))
    assert got.status_code == 200 and got.content == blob and got.headers["content-type"] == "application/zip"
    assert "attachment" in got.headers["content-disposition"]
    env.clock[0] += 61
    assert env.client.get(local_path(body["url"])).status_code == 404


@pytest.mark.parametrize("status", ["queued", "starting", "running", "session_boundary", "dreaming", "stopping"])
def test_an_unfinished_run_cannot_be_exported(env: Env, status: str) -> None:
    ids = seed(env, "public")
    seal(env, ids["run"], status=status)
    r = env.client.get(f"/api/v1/runs/{ids['run']}/export")
    assert (r.status_code, r.json()["code"]) == (409, "export_not_ready") and "url" not in r.text


@pytest.mark.parametrize("final", ["completed", "capped", "failed", "killed"])
def test_every_final_status_with_a_sealed_bundle_exports(env: Env, final: str) -> None:
    ids = seed(env, "public")
    seal(env, ids["run"], status=final)
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").status_code == 200


def test_a_finished_run_without_a_recorded_bundle_hash_or_blob_is_not_ready(env: Env) -> None:
    ids = seed(env, "public")
    env.admin.execute("update public.runs set status = 'completed' where id = %s", (ids["run"],))
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").json()["code"] == "export_not_ready"   # no state row
    env.store.put(f"runs/{ids['run']}/bundle.zip", make_bundle())
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").json()["code"] == "export_not_ready"   # blob but no hash
    seal(env, ids["run"], store=False)
    env.store.delete(f"runs/{ids['run']}/bundle.zip")
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").json()["code"] == "export_not_ready"   # hash but no blob


def test_the_redaction_scan_is_re_run_and_a_dirty_bundle_is_refused(env: Env) -> None:
    ids = seed(env, "public")
    dirty = make_bundle({"bundle.json": b"{}", "events.jsonl": b'{"data":"oops sk-ant-api03-' + b"A" * 40 + b'"}\n'})
    seal(env, ids["run"], dirty)                          # sealed earlier, before the pattern list could catch it
    r = env.client.get(f"/api/v1/runs/{ids['run']}/export")
    assert (r.status_code, r.json()["code"]) == (409, "export_refused")
    assert "sk-ant" not in r.text and "url" not in r.text and "events.jsonl" not in r.text
    # one dirty file among many, and a key hidden in a nested name's content
    seal(env, ids["run"], make_bundle({**{f"f{i}.txt": b"clean" for i in range(20)}, "memory/final.json": b"Bearer " + b"x" * 30}))
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").json()["code"] == "export_refused"


def test_a_bundle_that_does_not_match_its_recorded_hash_is_refused(env: Env) -> None:
    ids = seed(env, "public")
    seal(env, ids["run"], make_bundle(), sha="0" * 64)
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").json()["code"] == "export_refused"


def test_a_bundle_that_is_not_a_zip_is_refused(env: Env) -> None:
    ids = seed(env, "public")
    seal(env, ids["run"], b"not a zip at all")
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").json()["code"] == "export_refused"


def test_a_zip_bomb_is_refused_without_being_expanded(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = seed(env, "public")
    import api.app as app_mod
    monkeypatch.setattr(app_mod, "MAX_EXPORT_BYTES", 1024 * 1024)            # so the bomb can be small in the test
    bomb = make_bundle({"zeros.bin": bytes(8 * 1024 * 1024)})                 # 8 MiB of zeros: a few KB compressed
    assert len(bomb) < 100_000
    seal(env, ids["run"], bomb)

    def no_read(self: Any, *a: Any, **k: Any) -> bytes:
        raise AssertionError("the bomb was expanded")

    monkeypatch.setattr(zipfile.ZipFile, "read", no_read)
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").json()["code"] == "export_refused"


@pytest.mark.parametrize("visibility", ["private", "unlisted", "public"])
@pytest.mark.parametrize("who", ["anon", "owner", "other", "admin"])
def test_export_follows_run_visibility_with_the_same_404(env: Env, who: str, visibility: str) -> None:
    ids = seed(env, visibility)
    seal(env, ids["run"])
    r = env.client.get(f"/api/v1/runs/{ids['run']}/export", headers=hdr(ids, who))
    if visibility != "private" or who in ("owner", "admin"):
        assert r.status_code == 200
    else:
        missing = env.client.get(f"/api/v1/runs/{uuid.uuid4()}/export", headers=hdr(ids, who))
        assert (r.status_code, r.json()) == (missing.status_code, missing.json())


def test_export_is_limited_per_identity_more_tightly_than_reads(make_env: Any) -> None:
    env: Env = make_env(read_limit_per_min=1000)
    ids = seed(env, "public")
    seal(env, ids["run"])
    codes = [env.client.get(f"/api/v1/runs/{ids['run']}/export").status_code for _ in range(12)]
    assert codes == [200] * 10 + [429] * 2
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export", headers=bearer(ids["other"])).status_code == 200


def test_a_deleted_runs_export_is_gone(env: Env) -> None:
    ids = seed(env, "public")
    seal(env, ids["run"])
    env.admin.execute("update public.runs set deleted_at = now() where id = %s", (ids["run"],))
    assert env.client.get(f"/api/v1/runs/{ids['run']}/export").status_code == 404


def test_without_a_signer_frames_and_export_answer_503_not_a_crash(make_env: Any) -> None:
    env: Env = make_env(signer=False)
    ids = seed(env, "public")
    put_frames(env, ids["run"], [0])
    seal(env, ids["run"])
    for route in (f"/api/v1/runs/{ids['run']}/frames/0", f"/api/v1/runs/{ids['run']}/export"):
        r = env.client.get(route, follow_redirects=False)
        assert (r.status_code, r.json()["code"]) == (503, "internal"), route
    assert env.client.get("/_blob/x?exp=1&sig=2").status_code == 404           # no blob route without a local signer
