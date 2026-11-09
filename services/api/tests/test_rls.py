"""RLS matrix against the local Supabase database: anon, user A, user B, admin, service role."""

import json

import psycopg
import pytest

from conftest import Db

TABLES = ["profiles", "games", "start_states", "presets", "run_configs", "runs", "run_events_index",
          "memory_files", "checkpoints", "run_scores", "experiments", "audit_log", "reports", "run_ingest_state", "stream_tickets"]


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
    privileged = viewer in ("owner", "admin")
    for table in ("runs", "run_events_index", "memory_files", "checkpoints", "run_scores"):
        key = "id" if table == "runs" else "run_id"
        # unlisted = link-only: no table lists it to anyone but the owner and admins (the API serves it by id)
        expected = 1 if (privileged or visibility == "public") else 0
        assert len(db.q(f"select 1 from public.{table} where {key} = %s", run)) == expected, table
    cfg = db.q("select 1 from public.run_configs c where c.id = (select config_id from public.runs where id = %s)", run)
    assert len(cfg) == (1 if privileged or visibility == "public" else 0)


def test_soft_deleted_runs_are_hidden_from_everyone_but_owner_and_admin(db: Db) -> None:
    owner, other = db.new_user(), db.new_user()
    run = db.make_run(owner, "public", deleted_at="2026-01-01")
    db.as_(other)
    assert db.q("select 1 from public.runs where id = %s", run) == []
    db.as_(owner)
    assert len(db.q("select 1 from public.runs where id = %s", run)) == 1


CREATE = ("select api_private.create_run_request(%s::jsonb, %s, 'demo', 'private', 5, 100, 30, 'g', null) as id")
JSON = "{}"


def test_direct_inserts_are_denied_so_the_function_is_the_only_way_to_create_a_run(db: Db) -> None:
    a = db.new_user()
    db.as_(a)
    for stmt in (("insert into public.run_configs (owner_id, config_json, config_hash, mode) "
                  f"values ('{a}', '{{}}', '{'c' * 64}', 'demo')"),
                 ("insert into public.runs (owner_id, config_id, cap_steps, cap_tokens, cap_wall_s) "
                  f"select '{a}', gen_random_uuid(), 5, 100, 30"),
                 f"insert into public.reports (reporter_id, run_id, reason) select '{a}', gen_random_uuid(), 'x'",
                 f"insert into public.experiments (owner_id, title, variable) values ('{a}', 't', 'v')"):
        db.x("savepoint s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.x(stmt)
        db.x("rollback to savepoint s")
    run = db.q(CREATE, JSON, "c" * 64)[0]["id"]
    row = db.q("select owner_id, status, visibility, cap_steps from public.runs where id = %s", run)[0]
    assert (str(row["owner_id"]), row["status"], row["visibility"], row["cap_steps"]) == (a, "queued", "private", 5)


@pytest.mark.parametrize("table", ["run_configs", "runs", "reports", "experiments"])
def test_no_insert_privilege_on_creation_tables_for_user_roles(db: Db, table: str) -> None:
    """Mutation guard: re-granting INSERT (table or column level) to a user role fails here."""
    for role in ("anon", "authenticated"):
        assert db.q("select has_table_privilege(%s, %s, 'insert') p", role, f"public.{table}")[0]["p"] is False
        cols = db.q("select column_name from information_schema.columns where table_schema = 'public' "
                    "and table_name = %s", table)
        assert [c["column_name"] for c in cols
                if db.q("select has_column_privilege(%s, %s, %s, 'insert') p", role, f"public.{table}",
                        c["column_name"])[0]["p"]] == []
    assert db.q("select has_table_privilege('authenticated', 'public.experiments', 'update') p")[0]["p"] is False
    assert [c["column_name"] for c in db.q("select column_name from information_schema.columns where "
            "table_schema = 'public' and table_name = 'experiments'")
            if db.q("select has_column_privilege('authenticated', 'public.experiments', %s, 'update') p",
                    c["column_name"])[0]["p"]] == []


def test_the_create_function_takes_no_owner_and_needs_a_signed_in_caller(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    db.as_(a)
    run = db.q(CREATE, JSON, "c" * 64)[0]["id"]
    db.as_(b)
    assert db.q("select 1 from public.runs where id = %s", run) == []     # a's private run is not b's
    db.as_(None)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.q(CREATE, JSON, "c" * 64)
    db.x("rollback to savepoint s")
    db.conn.execute("reset role")                                        # a role with no sub claim has no uid
    db.conn.execute("select set_config('request.jwt.claims', '{\"role\": \"authenticated\"}', true)")
    db.conn.execute("set local role authenticated")
    db.x("savepoint s2")
    with pytest.raises(psycopg.errors.RaiseException, match="not_authenticated"):
        db.q(CREATE, JSON, "c" * 64)
    db.x("rollback to savepoint s2")


def test_create_function_enforces_caps_and_one_active_run(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    db.as_(a)
    for caps in ((0, 100, 30), (100001, 100, 30), (5, 0, 30), (5, 1_000_000_001, 30), (5, 100, 0), (5, 100, 86401)):
        db.x("savepoint s")
        with pytest.raises(psycopg.errors.RaiseException, match="caps_out_of_range"):
            db.q("select api_private.create_run_request('{}', %s, 'demo', 'private', %s, %s, %s, 'g', null)",
                 "c" * 64, *caps)
        db.x("rollback to savepoint s")
    db.q("select api_private.create_run_request('{}', %s, 'demo', 'private', 100000, 1000000000, 86400, 'g', null)",
         "c" * 64)                                                         # the maxima themselves are fine
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.RaiseException, match="active_run_exists"):
        db.q(CREATE, JSON, "c" * 64)
    db.x("rollback to savepoint s")
    db.as_(b)
    db.q(CREATE, JSON, "c" * 64)                                           # another user is not blocked
    db.as_service()
    db.x("update public.runs set status = 'completed' where owner_id = %s", a)
    db.as_(a)
    db.q(CREATE, JSON, "c" * 64)                                           # a finished run frees the slot


def test_the_run_table_has_check_constraints_for_the_cap_maxima(db: Db) -> None:
    a = db.new_user()
    for col, val in (("cap_steps", 100001), ("cap_tokens", 1_000_000_001), ("cap_wall_s", 86401)):
        db.x("savepoint s")
        with pytest.raises(psycopg.errors.CheckViolation):
            db.make_run(a, **{col: val})
        db.x("rollback to savepoint s")
        db.conn.execute("reset role")


def test_abandon_moves_only_the_callers_own_queued_run_to_failed(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    db.as_(a)
    run = db.q(CREATE, JSON, "c" * 64)[0]["id"]
    db.as_(b)
    assert db.q("select api_private.abandon_run_request(%s) ok", run)[0]["ok"] is False
    db.as_(a)
    assert db.q("select api_private.abandon_run_request(%s) ok", run)[0]["ok"] is True
    assert db.q("select api_private.abandon_run_request(%s) ok", run)[0]["ok"] is False     # not queued any more
    row = db.q("select status, end_reason, ended_at is not null done from public.runs where id = %s", run)[0]
    assert row == {"status": "failed", "end_reason": "submit_refused", "done": True}
    db.q(CREATE, JSON, "c" * 64)                                                          # slot is free again


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
    assert len(db.q("select * from public.audit_log where actor_id = %s", admin)) == 1


def test_reports_reporter_reads_own_admin_all_and_only_visible_runs_can_be_reported(db: Db) -> None:
    owner, a, b, admin = db.new_user(), db.new_user(), db.new_user(), db.new_user("admin")
    pub, priv = db.make_run(owner, "public"), db.make_run(owner, "private")
    db.as_(a)
    rid = db.q("select api_private.file_report(%s, 'spam') id", pub)[0]["id"]
    assert db.q("select api_private.file_report(%s, 'again') id", pub)[0]["id"] == rid     # idempotent while open
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.RaiseException, match="run_not_found"):
        db.q("select api_private.file_report(%s, 'probe')", priv)
    db.x("rollback to savepoint s")
    assert len(db.q("select 1 from public.reports")) == 1
    db.as_(b)
    assert db.q("select 1 from public.reports") == []
    db.as_(admin)
    assert len(db.q("select 1 from public.reports")) == 1


def test_file_report_refuses_self_reports_and_limits_reporters_to_ten_an_hour(db: Db) -> None:
    owner, a = db.new_user(), db.new_user()
    mine = db.make_run(owner, "public")
    db.as_(owner)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.RaiseException, match="cannot_report_own_run"):
        db.q("select api_private.file_report(%s, 'me')", mine)
    db.x("rollback to savepoint s")
    runs = [db.make_run(owner, "public") for _ in range(11)]
    db.as_(a)
    for r in runs[:10]:
        db.q("select api_private.file_report(%s, 'x')", r)
    db.q("select api_private.file_report(%s, 'dup')", runs[0])                 # a duplicate is not a new report
    db.x("savepoint s2")
    with pytest.raises(psycopg.errors.RaiseException, match="report_rate_limited"):
        db.q("select api_private.file_report(%s, 'x')", runs[10])
    db.x("rollback to savepoint s2")
    for reason in ("", "x" * 1001):
        db.x("savepoint s3")
        with pytest.raises(psycopg.errors.RaiseException, match="reason_invalid"):
            db.q("select api_private.file_report(%s, %s)", runs[10], reason)
        db.x("rollback to savepoint s3")


def test_experiments_follow_owner_and_visibility(db: Db) -> None:
    a, b = db.new_user(), db.new_user()
    db.as_service()
    for vis in ("private", "unlisted", "public"):
        db.x("insert into public.experiments (owner_id, title, variable, visibility) values (%s, %s, 'preset', %s)",
             a, vis, vis)
    for who in (b, None):                                             # unlisted = link-only: not listed to others
        db.as_(who)
        titles = {r["title"] for r in db.q("select title from public.experiments where owner_id = %s", a)}
        assert titles == {"public"}
    db.as_(a)
    assert {r["title"] for r in db.q("select title from public.experiments")} == {"private", "unlisted", "public"}
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):             # no direct writes by anyone, owner included
        db.x("update public.experiments set title = 'pwned'")
    db.x("rollback to savepoint s")


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


def test_ingest_state_is_invisible_to_every_user_role(db: Db) -> None:
    owner = db.new_user()
    run = db.make_run(owner, "public")
    db.as_service()
    db.x("insert into public.run_ingest_state (run_id) values (%s)", run)
    for who in (owner, None):
        db.as_(who)
        db.x("savepoint s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.q("select * from public.run_ingest_state")
        db.x("rollback to savepoint s")


def test_get_visible_run_returns_the_link_only_rows_the_runs_table_does_not_list(db: Db) -> None:
    owner, other, admin = db.new_user(), db.new_user(), db.new_user("admin")
    priv, unl, pub = (db.make_run(owner, v) for v in ("private", "unlisted", "public"))
    gone = db.make_run(owner, "unlisted", deleted_at="2026-01-01")

    def got(run: str) -> bool:
        return len(db.q("select id from api_private.get_visible_run(%s)", run)) == 1

    for who, expect in ((owner, (True, True, True, True)), (admin, (True, True, True, True)),
                        (other, (False, True, True, False)), (None, (False, True, True, False))):
        db.as_(who)
        assert (got(priv), got(unl), got(pub), got(gone)) == expect, who
    for who in (None, other):                                                       # the table lists public only
        db.as_(who)
        listed = {str(r["id"]) for r in db.q("select id from public.runs")}
        assert pub in listed and not listed & {priv, unl, gone}
    assert db.q("select id from api_private.get_visible_run(gen_random_uuid())") == []


def test_unlisted_run_configs_are_not_listable_through_the_data_api(db: Db) -> None:
    owner, other = db.new_user(), db.new_user()
    unl, pub = db.make_run(owner, "unlisted"), db.make_run(owner, "public")
    db.as_service()
    cfg = {r: db.q("select config_id from public.runs where id = %s", r)[0]["config_id"] for r in (unl, pub)}
    for who in (None, other):
        db.as_(who)
        listed = {r["id"] for r in db.q("select id from public.run_configs")}
        assert cfg[pub] in listed and cfg[unl] not in listed
    db.as_(owner)
    assert {cfg[unl], cfg[pub]} <= {r["id"] for r in db.q("select id from public.run_configs")}


CHILDREN = ["run_events_index", "memory_files", "checkpoints", "run_scores"]


def seed_children(db: Db, run: str) -> None:
    db.as_service()
    db.x("insert into public.run_events_index values (%s, 1, 'run.start', 0, now(), 0)", run)
    db.x("insert into public.memory_files values (%s, 'a.md', 1, 0, 'UNLISTED-MEMORY-CONTENT', 23, false)", run)
    db.x("insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, config_hash) "
         "values (%s, 0, 'k', 1, 0, %s)", run, "b" * 64)
    db.x("insert into public.run_scores values (%s, 1, 1, 1, 1, 1, 0, 0, false)", run)


@pytest.mark.parametrize("table", CHILDREN)
@pytest.mark.parametrize("viewer", ["anon", "other"])
def test_no_child_table_lists_an_unlisted_runs_rows(db: Db, table: str, viewer: str) -> None:
    owner, other = db.new_user(), db.new_user()
    unlisted, public = db.make_run(owner, "unlisted"), db.make_run(owner, "public")
    seed_children(db, unlisted)
    seed_children(db, public)
    db.as_(None if viewer == "anon" else other)
    listed = {str(r["run_id"]) for r in db.q(f"select run_id from public.{table}")}
    assert public in listed and unlisted not in listed                                         # not an enumeration
    db.as_(owner)
    listed = {str(r["run_id"]) for r in db.q(f"select run_id from public.{table}")}
    assert {unlisted, public} <= listed


def test_only_the_expected_policies_still_use_the_by_id_visibility_helper(db: Db) -> None:
    """run_visible() admits unlisted runs, which is right for a lookup by id and wrong for a row policy that lists.
    The allowed set is empty: nothing row-level may use it. Putting it back on any child table fails here."""
    using = db.q("select tablename from pg_policies where schemaname = 'public' and qual like %s", "%run_visible%")
    assert {r["tablename"] for r in using} == set()
    listable = db.q("select tablename from pg_policies where schemaname = 'public' and qual like %s", "%run_listable%")
    assert {r["tablename"] for r in listable} == set(CHILDREN)


def test_the_readers_serve_unlisted_runs_by_id_and_nothing_hidden(db: Db) -> None:
    owner, other, admin = db.new_user(), db.new_user(), db.new_user("admin")
    priv, unl, pub, gone = (db.make_run(owner, v) for v in ("private", "unlisted", "public", "unlisted"))
    db.as_service()
    db.x("update public.runs set deleted_at = now() where id = %s", gone)
    for r in (priv, unl, pub, gone):
        seed_children(db, r)

    def seen(run: str) -> tuple[int, int, int, int, int]:
        return (len(db.q("select * from api_private.run_memory_tree(%s, 100)", run)),
                len(db.q("select * from api_private.run_memory_version(%s, 'a.md', null)", run)),
                len(db.q("select * from api_private.run_memory_version(%s, 'a.md', 1)", run)),
                len(db.q("select * from api_private.run_checkpoints(%s)", run)),
                len(db.q("select * from api_private.run_score(%s)", run)))

    for who, expect in ((None, {priv: 0, unl: 1, pub: 1, gone: 0}), (other, {priv: 0, unl: 1, pub: 1, gone: 0}),
                        (owner, {priv: 1, unl: 1, pub: 1, gone: 0}), (admin, {priv: 1, unl: 1, pub: 1, gone: 0})):
        db.as_(who)
        for run, n in expect.items():
            assert seen(run) == (n,) * 5, (who, run)
    db.as_(other)
    assert db.q("select * from api_private.run_memory_version(%s, 'nope.md', null)", unl) == []
    assert db.q("select content from api_private.run_memory_version(%s, 'a.md', null)", unl) == [
        {"content": "UNLISTED-MEMORY-CONTENT"}]


def test_the_fork_readers_follow_get_visible_run(db: Db) -> None:
    owner, other, admin = db.new_user(), db.new_user(), db.new_user("admin")
    priv, unl, pub, gone = (db.make_run(owner, v) for v in ("private", "unlisted", "public", "unlisted"))
    db.as_service()
    db.x("update public.runs set deleted_at = now() where id = %s", gone)
    cps = {}
    for r in (priv, unl, pub, gone):
        cps[r] = db.q("insert into public.checkpoints (run_id, step, savestate_key, memory_version, transcript_offset, "
                      "config_hash) values (%s, 1, 'k', 1, 0, %s) returning id", r, "b" * 64)[0]["id"]

    def seen(run: str, cp: str) -> tuple[int, int]:
        return (len(db.q("select * from api_private.run_config(%s)", run)),
                len(db.q("select * from api_private.run_checkpoint(%s, %s)", run, cp)))

    for who, expect in ((owner, {priv: 1, unl: 1, pub: 1, gone: 0}), (admin, {priv: 1, unl: 1, pub: 1, gone: 0}),
                        (other, {priv: 0, unl: 1, pub: 1, gone: 0})):
        db.as_(who)
        for run, n in expect.items():
            assert seen(run, cps[run]) == (n, n), (who, run)
    db.as_(other)
    assert seen(unl, cps[pub]) == (1, 0)                    # a checkpoint of another run is not found under this run
    assert seen(pub, cps[priv]) == (1, 0)                   # ...in particular not a hidden run's
    db.as_(None)
    assert len(db.q("select * from api_private.run_config(%s)", pub)) == 1        # a public run's configuration is public (migration 17)
    assert db.q("select * from api_private.run_config(%s)", priv) == []
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):                     # but forking needs a signed-in user: no anon execute
        db.q("select * from api_private.run_checkpoint(%s, %s)", pub, cps[pub])
    db.x("rollback to savepoint s")


def test_stream_ticket_functions_check_visibility_and_the_account_and_consume_on_redeem(db: Db) -> None:
    import hashlib
    owner, other, ghost = db.new_user(), db.new_user(), db.new_user()
    priv, pub = db.make_run(owner, "private"), db.make_run(owner, "public")
    db.as_service()
    db.x("update public.profiles set erased_at = now() where id = %s", ghost)
    h = [hashlib.sha256(f"t{i}".encode()).hexdigest() for i in range(6)]

    def issue(who: str | None, run: str, digest: str) -> None:
        db.as_(who)
        db.q("select api_private.issue_stream_ticket(%s, %s)", run, digest)

    for who, run, digest in ((owner, priv, h[0]), (other, pub, h[1]), (None, pub, h[2])):
        issue(who, run, digest)                                                     # allowed
    for who, run, digest, err in ((other, priv, h[3], "run_not_found"), (None, priv, h[3], "run_not_found"),
                                  (ghost, pub, h[4], "account_erased")):
        db.x("savepoint s")
        with pytest.raises(psycopg.errors.RaiseException, match=err):
            issue(who, run, digest)
        db.x("rollback to savepoint s")
    db.as_service()
    stored = {r["token_sha256"]: r["user_id"] for r in db.q("select token_sha256, user_id from public.stream_tickets")}
    assert set(stored) == {h[0], h[1], h[2]} and str(stored[h[1]]) == other and stored[h[2]] is None
    db.as_(None)
    got = db.q("select * from api_private.redeem_stream_ticket(%s)", h[1])
    assert [(str(r["run_id"]), str(r["user_id"])) for r in got] == [(pub, other)]
    assert db.q("select * from api_private.redeem_stream_ticket(%s)", h[1]) == []        # once
    db.as_(None)
    db.x("savepoint s")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):                              # not a table anyone can read
        db.q("select * from public.stream_tickets")
    db.x("rollback to savepoint s")


# Columns of these types can carry other tables' ids or user-chosen structure. Anything readable by a user role must be
# listed here on purpose, with the reason.
READABLE_STRUCTURED_COLUMNS = {
    ("run_configs", "config_json"),     # a public run's configuration is public; policy limits rows to public runs
    ("runs", "fork_patch"),             # the config diff of a run the viewer may list
}


def test_no_array_or_jsonb_column_is_readable_by_a_user_role_unless_allowlisted(db: Db) -> None:
    """Catalogue sweep: experiments.run_ids once leaked private run ids this way."""
    cols = db.q("select c.table_name t, c.column_name col from information_schema.columns c "
                "where c.table_schema = 'public' and (c.data_type in ('jsonb', 'json') or c.data_type = 'ARRAY')")
    found = {(c["t"], c["col"]) for c in cols
             for role in ("anon", "authenticated")
             if db.q("select has_column_privilege(%s, %s, %s, 'select') p", role, f"public.{c['t']}", c["col"])[0]["p"]}
    assert found == READABLE_STRUCTURED_COLUMNS, found ^ READABLE_STRUCTURED_COLUMNS


def test_experiments_run_ids_has_no_select_privilege_for_anyone_but_the_service(db: Db) -> None:
    for role in ("anon", "authenticated"):
        assert db.q("select has_column_privilege(%s, 'public.experiments', 'run_ids', 'select') p", role)[0]["p"] is False
        assert db.q("select has_table_privilege(%s, 'public.experiments', 'select') p", role)[0]["p"] is False
        assert db.q("select has_column_privilege(%s, 'public.experiments', 'title', 'select') p", role)[0]["p"] is True
