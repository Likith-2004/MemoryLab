import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from accounts.app import create_app
from accounts.eraser import TOMBSTONE_ID, Erased, RunsStillActive, UpstreamUnavailable
from conftest import DB_URL, World

TOKEN = b"t" * 40


def seed(w: World) -> dict[str, str]:
    me, other = w.user(), w.user()
    ids = {
        "me": me, "other": other,
        "kept": w.run(me, "public", verified=True),
        "unlisted_verified": w.run(me, "unlisted", verified=True),
        "public_unverified": w.run(me, "public", verified=False),
        "public_noscore": w.run(me, "public"),
        "private": w.run(me, "private", verified=None),
        "already_deleted": w.run(me, "public", verified=True),
        "other_pub": w.run(other, "public", verified=True),
        "other_priv": w.run(other, "private"),
    }
    w.conn.execute("update public.runs set deleted_at = now() where id = %s", (ids["already_deleted"],))
    return ids


def test_only_verified_public_runs_survive_and_they_are_anonymised(world: World) -> None:
    ids = seed(world)
    assert world.eraser.erase(ids["me"]) == Erased(kept=1, removed=5)
    kept = world.row(ids["kept"])
    assert kept == {"owner": TOMBSTONE_ID, "title": None, "deleted": False, "status": "completed",
                    "cfg": '{"secret_note": "mine"}'}                          # the benchmark stays reproducible
    for name in ("unlisted_verified", "public_unverified", "public_noscore", "private", "already_deleted"):
        row = world.row(ids[name])
        assert (row["owner"], row["title"], row["deleted"]) == (TOMBSTONE_ID, None, True), name
        assert name == "already_deleted" or row["cfg"] == "{}", name              # config JSON of removed runs is blanked


def test_nothing_of_another_user_changes(world: World) -> None:
    ids = seed(world)
    world.eraser.erase(ids["me"])
    for name in ("other_pub", "other_priv"):
        row = world.row(ids[name])
        assert (row["owner"], row["title"], row["deleted"], row["cfg"]) == (
            ids["other"], "my title", False, '{"secret_note": "mine"}'), name
    assert world.conn.execute("select count(*) n from public.profiles where id = %s", (ids["other"],)).fetchone()["n"] == 1  # type: ignore[index]


def test_the_account_is_gone_and_no_trace_of_its_id_is_left_in_public_tables(world: World) -> None:
    ids = seed(world)
    world.conn.execute("insert into public.experiments (owner_id, title, variable) values (%s, 't', 'v')", (ids["me"],))
    world.conn.execute("insert into public.reports (reporter_id, run_id, reason) values (%s, %s, 'x')",
                       (ids["me"], ids["other_pub"]))
    before = world.conn.execute("select count(*) n from public.audit_log where action = 'account.erased'").fetchone()["n"]  # type: ignore[index]
    world.eraser.erase(ids["me"])
    assert world.auth.deleted == [ids["me"]]
    assert world.conn.execute("select count(*) n from public.profiles where id = %s", (ids["me"],)).fetchone()["n"] == 0  # type: ignore[index]
    tables = [r["relname"] for r in world.conn.execute(
        "select relname from pg_class where relnamespace = 'public'::regnamespace and relkind = 'r'").fetchall()]
    for t in tables:
        dump = world.conn.execute(f"select coalesce(string_agg(x::text, ' '), '') s from public.{t} x").fetchone()["s"]  # type: ignore[index]
        assert ids["me"] not in dump, t
        assert "secret_note" not in dump or t == "run_configs", t
    audit = world.conn.execute("select actor_id, target from public.audit_log where action = 'account.erased'").fetchall()
    assert len(audit) == before + 1 and all(a["actor_id"] is None and a["target"] is None for a in audit)


def test_active_runs_are_killed_first_with_the_reason(world: World) -> None:
    me = world.user()
    running, queued = world.run(me, status="running"), world.run(me, "public", verified=True, status="queued")
    world.run(me)
    world.eraser.erase(me)
    assert sorted(world.killer.killed) == sorted([(running, me, "account_erased"), (queued, me, "account_erased")])


def test_a_run_that_will_not_stop_blocks_the_erase_and_changes_nothing(world: World) -> None:
    me = world.user()
    run = world.run(me, "public", verified=True, status="running")
    world.killer.stuck = True
    with pytest.raises(RunsStillActive):
        world.eraser.erase(me)
    assert world.auth.deleted == [] and world.row(run)["owner"] == me and world.row(run)["title"] == "my title"
    assert world.ticks >= 3                                                   # it waited for the kill, then gave up
    world.killer.stuck = False
    world.conn.execute("update public.runs set status = 'killed' where id = %s", (run,))
    assert world.eraser.erase(me).kept == 1                                   # a retry succeeds


def test_a_second_erase_after_a_partial_failure_finishes_the_job(world: World) -> None:
    me = world.user()
    run = world.run(me, "public", verified=True)
    world.auth.keep = True                                                    # GoTrue "fails": the user remains
    world.eraser.erase(me)
    assert world.row(run)["owner"] == TOMBSTONE_ID
    world.auth.keep = False
    assert world.eraser.erase(me) == Erased(0, 0)                             # runs are already moved; user now deleted
    assert world.auth.deleted == [me, me]
    assert world.conn.execute("select count(*) n from public.profiles where id = %s", (me,)).fetchone()["n"] == 0  # type: ignore[index]


def test_an_erased_account_cannot_start_a_run_with_a_token_that_is_still_valid(world: World) -> None:
    me = world.user()
    world.auth.keep = True
    world.eraser.erase(me)
    with psycopg.connect(DB_URL) as c:
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": me, "role": "authenticated"}),))
        c.execute("set local role authenticated")
        with pytest.raises(psycopg.errors.RaiseException, match="account_erased"):
            c.execute("select api_private.create_run_request('{}', %s, 'demo', 'private', 5, 100, 30, null)", ("c" * 64,))


def test_the_tombstone_cannot_be_erased(world: World) -> None:
    with pytest.raises(ValueError, match="tombstone"):
        world.eraser.erase(TOMBSTONE_ID)
    assert world.conn.execute("select count(*) n from public.profiles where id = %s", (TOMBSTONE_ID,)).fetchone()["n"] == 1  # type: ignore[index]


def test_the_tombstone_profile_exists_and_cannot_sign_in(world: World) -> None:
    row = world.conn.execute("select u.encrypted_password, u.email_confirmed_at, u.banned_until > now() banned, "
                             "p.erased_at is not null erased from auth.users u join public.profiles p on p.id = u.id "
                             "where u.id = %s", (TOMBSTONE_ID,)).fetchone()
    assert row == {"encrypted_password": None, "email_confirmed_at": None, "banned": True, "erased": True}


# ---------------------------------------------------------------- the HTTP surface

def client(world: World) -> TestClient:
    return TestClient(create_app(world.eraser, TOKEN), raise_server_exceptions=False)


def test_the_endpoint_needs_the_internal_token(world: World) -> None:
    c = client(world)
    me = world.user()
    for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": "Basic " + TOKEN.decode()}):
        r = c.post("/internal/accounts/erase", json={"user_id": me}, headers=headers)
        assert r.status_code == 401
    assert world.auth.deleted == []
    r = c.post("/internal/accounts/erase", json={"user_id": me}, headers={"authorization": "Bearer " + TOKEN.decode()})
    assert r.status_code == 200 and r.json() == {"kept": 0, "removed": 0}


def test_the_endpoint_rejects_bad_ids_and_maps_errors(world: World) -> None:
    c = client(world)
    h = {"authorization": "Bearer " + TOKEN.decode()}
    for bad in ("nope", TOMBSTONE_ID, "a\x00b"):
        assert c.post("/internal/accounts/erase", json={"user_id": bad}, headers=h).status_code == 422
    assert c.post("/internal/accounts/erase", json={"user_id": TOMBSTONE_ID, "x": 1}, headers=h).status_code == 422
    me = world.user()
    world.run(me, status="running")
    world.killer.stuck = True
    r = c.post("/internal/accounts/erase", json={"user_id": me}, headers=h)
    assert (r.status_code, r.json()["code"], r.headers["retry-after"]) == (409, "runs_still_active", "5")

    def down(_: str, __: str, ___: str) -> str:
        raise UpstreamUnavailable("orchestrator")

    world.killer.kill = down  # type: ignore[method-assign]
    r = c.post("/internal/accounts/erase", json={"user_id": me}, headers=h)
    assert (r.status_code, r.json()["code"]) == (503, "upstream_unavailable")


def test_the_database_refuses_to_delete_the_tombstone_profile(world: World) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="tombstone_protected"):
        world.conn.execute("delete from auth.users where id = %s", (TOMBSTONE_ID,))
    assert world.conn.execute("select count(*) n from public.profiles where id = %s", (TOMBSTONE_ID,)).fetchone()["n"] == 1  # type: ignore[index]
