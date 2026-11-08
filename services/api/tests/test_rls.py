"""RLS matrix against the local Supabase database: anon, user A, user B, admin, service role."""

import json

import psycopg
import pytest

from conftest import Db

TABLES = ["profiles", "games", "start_states", "presets", "run_configs", "runs", "run_events_index",
          "memory_files", "checkpoints", "run_scores", "experiments", "audit_log", "reports"]


def test_every_public_table_has_forced_rls(db: Db) -> None:
    rows = db.q("select relname, relrowsecurity r, relforcerowsecurity f from pg_class "
                "where relnamespace = 'public'::regnamespace and relkind = 'r'")
    assert {r["relname"] for r in rows} == set(TABLES)
    assert all(r["r"] and r["f"] for r in rows)


def test_auth_uid_follows_the_jwt_claims(db: Db) -> None:
    a = db.new_user()
    db.as_(a)
    assert str(db.q("select auth.uid() u")[0]["u"]) == a
    db.as_(None)
    assert db.q("select auth.uid() u")[0]["u"] is None


def test_a_profile_row_is_created_for_each_new_auth_user(db: Db) -> None:
    a = db.new_user()
    assert db.q("select role from public.profiles where id = %s", a) == [{"role": "user"}]


@pytest.mark.parametrize(("viewer", "visibility", "sees"), [
    ("owner", "private", True), ("other", "private", False), ("anon", "private", False), ("admin", "private", True),
    ("owner", "unlisted", True), ("other", "unlisted", True), ("anon", "unlisted", True), ("admin", "unlisted", True),
    ("owner", "public", True), ("other", "public", True), ("anon", "public", True), ("admin", "public", True),
])
def test_run_and_its_children_follow_visibility(db: Db, viewer: str, visibility: str, sees: bool) -> None:
    owner, other, admin = db.new_user(), db.new_user(), db.new_user("admin")
    run = db.make_run(owner, visibility)
    db.as_service()
    db.x("insert into public.run_events_index values (%s, 1, 'run.start', 0, now(), 0)", run)
    db.x("insert into public.memory_files values (%s, 'a.md', 1, 0, 'x', 1, false)", run)
    db.x("insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, config_hash) "
         "values (%s, 0, 'k', 1, 0, %s)", run, "b" * 64)
    db.x("insert into public.run_scores values (%s, 1, 1, 1, 1, 1, 0, 0, false)", run)
    db.as_({"owner": owner, "other": other, "anon": None, "admin": admin}[viewer])
    expected = 1 if sees else 0
    for table in ("runs", "run_events_index", "memory_files", "checkpoints", "run_scores"):
        key = "id" if table == "runs" else "run_id"
        assert len(db.q(f"select 1 from public.{table} where {key} = %s", run)) == expected, table
    cfg = db.q("select 1 from public.run_configs")
    assert len(cfg) == (1 if viewer in ("owner", "admin") or sees else 0)


def test_soft_deleted_runs_are_hidden_from_everyone_but_owner_and_admin(db: Db) -> None:
    owner, other = db.new_user(), db.new_user()
    run = db.make_run(owner, "public", deleted_at="2026-01-01")
    db.as_(other)
    assert db.q("select 1 from public.runs where id = %s", run) == []
    db.as_(owner)
    assert len(db.q("select 1 from public.runs where id = %s", run)) == 1


def test_a_user_can_create_a_queued_run_request_for_themselves_only(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    db.as_(a)
    cfg = db.q("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
               "values (%s, '{}', %s, 'demo') returning id", a, "c" * 64)[0]["id"]
    db.x("insert into public.runs (owner_id, config_id, cap_steps, cap_tokens, cap_wall_s) "
         "values (%s, %s, 5, 100, 30)", a, cfg)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):             # not as someone else
        db.x("savepoint s")
        db.x("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
             "values (%s, '{}', %s, 'demo')", b, "c" * 64)
    db.x("rollback to savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):             # nor a run claiming progress
        db.x("savepoint s2")
        db.x("insert into public.runs (owner_id, config_id, cap_steps, cap_tokens, cap_wall_s, status) "
             "values (%s, %s, 5, 100, 30, 'completed')", a, cfg)
    db.x("rollback to savepoint s2")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.x("savepoint s3")
        db.x("insert into public.runs (owner_id, config_id, cap_steps, cap_tokens, cap_wall_s, steps) "
             "values (%s, %s, 5, 100, 30, 99)", a, cfg)
    db.x("rollback to savepoint s3")


def test_the_run_request_cannot_be_created_for_another_owner(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    db.as_(a)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        db.x("insert into public.runs (owner_id, config_id, cap_steps, cap_tokens, cap_wall_s) "
             "select %s, gen_random_uuid(), 5, 100, 30", b)
    db.x("rollback to savepoint s")


def test_owner_edits_title_and_visibility_but_not_status_counters_or_owner(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    run = db.make_run(a)
    db.as_(a)
    db.x("update public.runs set title = 't', visibility = 'public' where id = %s", run)
    for col, val in [("status", "'completed'"), ("steps", "5"), ("tokens", "5"), ("owner_id", f"'{b}'"),
                     ("end_reason", "'x'"), ("cap_steps", "999999"), ("deleted_at", "now()")]:
        db.x("savepoint s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.x(f"update public.runs set {col} = {val} where id = %s", run)
        db.x("rollback to savepoint s")
    db.as_service()
    row = db.q("select title, visibility, status, steps from public.runs where id = %s", run)[0]
    assert row == {"title": "t", "visibility": "public", "status": "queued", "steps": 0}


def test_other_users_and_anon_cannot_update_or_delete_a_run(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    run = db.make_run(a, "public")
    for who in (b, None):
        db.as_(who)
        db.x("savepoint s")
        try:
            db.x("update public.runs set title = 'pwned' where id = %s", run)
        except psycopg.errors.InsufficientPrivilege:
            db.x("rollback to savepoint s")
        db.x("savepoint d")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.x("delete from public.runs where id = %s", run)
        db.x("rollback to savepoint d")
    db.as_service()
    assert db.q("select title from public.runs where id = %s", run) == [{"title": None}]


def test_a_verified_benchmark_run_cannot_go_private(db: Db) -> None:
    a = db.new_user()
    run = db.make_run(a, "public")
    db.as_service()
    db.x("insert into public.run_scores values (%s, 3, 3, 1, 1, 1, 0, 0, true)", run)
    db.as_(a)
    db.x("update public.runs set visibility = 'unlisted' where id = %s", run)   # still allowed
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.RaiseException, match="benchmark_visibility_locked"):
        db.x("update public.runs set visibility = 'private' where id = %s", run)
    db.x("rollback to savepoint s")


@pytest.mark.parametrize("table", ["run_events_index", "memory_files", "checkpoints", "run_scores", "games",
                                   "presets", "start_states", "audit_log"])
def test_user_jwts_cannot_write_service_only_tables(db: Db, table: str) -> None:
    a = db.new_user()
    db.as_(a)
    for stmt in (f"insert into public.{table} overriding system value select * from public.{table} limit 0",
                 f"delete from public.{table}", f"update public.{table} set {_col(db, table)} = {_col(db, table)}"):
        db.x("savepoint s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.x(stmt)
        db.x("rollback to savepoint s")


def _col(db: Db, table: str) -> str:
    return db.conn.execute("select column_name from information_schema.columns where table_schema = 'public' "
                           "and table_name = %s order by ordinal_position desc limit 1", (table,)).fetchone()["column_name"]  # type: ignore[index]


def test_games_and_start_states_are_visible_only_when_the_game_is_public(db: Db) -> None:
    a = db.new_user()
    db.as_service()
    pub = db.q("insert into public.games (name, rom_sha256, licence, state_map_version, public) "
               "values ('p', %s, 'CC0', '1', true) returning id", "d" * 64)[0]["id"]
    hid = db.q("insert into public.games (name, rom_sha256, licence, state_map_version, public) "
               "values ('h', %s, 'proprietary', '1', false) returning id", "e" * 64)[0]["id"]
    for g in (pub, hid):
        db.x("insert into public.start_states (game_id, savestate_sha256, label) values (%s, %s, 'start')", g, "f" * 64)
    for who in (None, a):
        db.as_(who)
        assert db.q("select name from public.games") == [{"name": "p"}]
        assert [str(r["game_id"]) for r in db.q("select game_id from public.start_states")] == [str(pub)]


def test_profiles_read_self_or_admin_and_handle_is_the_only_editable_column(db: Db) -> None:
    a, b, admin = db.new_user(), db.new_user(), db.new_user("admin")
    db.as_(a)
    assert [str(r["id"]) for r in db.q("select id from public.profiles")] == [a]
    db.x("update public.profiles set handle = 'alice_1' where id = %s", a)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):             # no self-promotion
        db.x("update public.profiles set role = 'admin' where id = %s", a)
    db.x("rollback to savepoint s")
    db.x("update public.profiles set handle = 'stolen' where id = %s", b)     # RLS filters: no rows touched
    db.as_(admin)
    assert len(db.q("select id from public.profiles where id in (%s, %s)", a, b)) == 2
    assert db.q("select handle from public.profiles where id = %s", b) == [{"handle": None}]
    db.as_(None)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.q("select * from public.profiles")
    db.x("rollback to savepoint s")


def test_admin_role_comes_from_the_table_not_the_jwt_claims(db: Db) -> None:
    a, owner = db.new_user(), db.new_user()
    run = db.make_run(owner)
    db.as_(a)
    db.x("select set_config('request.jwt.claims', %s, true)",
         json.dumps({"sub": a, "role": "authenticated", "app_metadata": {"role": "admin"}, "user_role": "admin"}))
    assert db.q("select 1 from public.runs where id = %s", run) == []
    assert db.q("select public.is_admin() v") == [{"v": False}]


def test_audit_log_is_admin_read_only(db: Db) -> None:
    a, admin = db.new_user(), db.new_user("admin")
    db.as_service()
    db.x("insert into public.audit_log (actor_id, action, target) values (%s, 'breaker.set', 'x')", admin)
    db.as_(a)
    assert db.q("select * from public.audit_log") == []
    db.as_(admin)
    assert len(db.q("select * from public.audit_log")) == 1


def test_reports_reporter_reads_own_admin_all_and_only_visible_runs_can_be_reported(db: Db) -> None:
    owner, a, b, admin = db.new_user(), db.new_user(), db.new_user(), db.new_user("admin")
    pub, priv = db.make_run(owner, "public"), db.make_run(owner, "private")
    db.as_(a)
    db.x("insert into public.reports (reporter_id, run_id, reason) values (%s, %s, 'spam')", a, pub)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        db.x("insert into public.reports (reporter_id, run_id, reason) values (%s, %s, 'probe')", a, priv)
    db.x("rollback to savepoint s")
    db.x("savepoint s2")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):             # no spoofed reporter, no status writes
        db.x("insert into public.reports (reporter_id, run_id, reason) values (%s, %s, 'x')", b, pub)
    db.x("rollback to savepoint s2")
    assert len(db.q("select 1 from public.reports")) == 1
    db.as_(b)
    assert db.q("select 1 from public.reports") == []
    db.as_(admin)
    assert len(db.q("select 1 from public.reports")) == 1


def test_experiments_follow_owner_and_visibility(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    db.as_(a)
    for vis in ("private", "unlisted", "public"):
        db.x("insert into public.experiments (owner_id, title, variable, visibility) values (%s, %s, 'preset', %s)",
             a, vis, vis)
    db.as_(b)
    assert {r["title"] for r in db.q("select title from public.experiments")} == {"unlisted", "public"}
    db.x("update public.experiments set title = 'pwned'")                  # RLS: no rows match
    db.as_(a)
    assert "pwned" not in {r["title"] for r in db.q("select title from public.experiments")}


def test_service_role_can_write_everything_users_cannot(db: Db) -> None:
    a = db.new_user()
    run = db.make_run(a)
    db.as_service()
    db.x("update public.runs set status = 'running', steps = 3 where id = %s", run)
    assert db.q("select status, steps from public.runs where id = %s", run) == [{"status": "running", "steps": 3}]


def test_soft_delete_function_is_owner_only_and_not_callable_by_anon(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    run = db.make_run(a, "public")
    db.as_(b)
    assert db.q("select public.soft_delete_run(%s) v", run) == [{"v": False}]
    db.as_(None)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.q("select public.soft_delete_run(%s)", run)
    db.x("rollback to savepoint s")
    db.as_(a)
    assert db.q("select public.soft_delete_run(%s) v", run) == [{"v": True}]
    db.as_service()
    assert db.q("select deleted_at is not null d from public.runs where id = %s", run) == [{"d": True}]
