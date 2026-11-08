"""Erase an account: kill its active runs, anonymise what the benchmark must keep, delete the rest, remove the
auth user (which ends every session and refresh token).

Why this is not plain `delete from auth.users`: profiles -> runs is ON DELETE CASCADE, so that would delete the
verified public benchmark runs too. They are first moved to the tombstone profile (title cleared); every other
run is soft-deleted (`deleted_at`) and moved to the tombstone as well, so the artifact-purge job still finds the
rows after the profile is gone. Their configuration JSON is blanked."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("accounts")

TOMBSTONE_ID = "00000000-0000-0000-0000-00000000dead"
ACTIVE = ("queued", "starting", "running", "session_boundary", "dreaming", "stopping")


class RunKiller(Protocol):
    def kill(self, run_id: str, actor: str, reason: str) -> str: ...


class AuthAdmin(Protocol):
    def delete_user(self, user_id: str) -> None: ...


class RunsStillActive(Exception):
    """A run is still stopping (or was started meanwhile); the caller retries shortly."""


class UpstreamUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Erased:
    kept: int          # verified public runs, now owned by the tombstone
    removed: int       # runs soft-deleted


class GoTrueAdmin:
    def __init__(self, auth_url: str, service_key: str, timeout_s: float = 10.0) -> None:
        self._url = auth_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout_s, headers={"apikey": service_key,
                                                              "authorization": f"Bearer {service_key}"})

    def delete_user(self, user_id: str) -> None:
        try:
            resp = self._http.request("DELETE", f"{self._url}/admin/users/{user_id}",
                                      json={"should_soft_delete": False})
        except httpx.HTTPError:
            raise UpstreamUnavailable("auth") from None
        if resp.status_code not in (200, 204, 404):             # 404: already gone, which is the goal
            raise UpstreamUnavailable("auth")


class HttpRunKiller:
    def __init__(self, base_url: str, token: bytes, timeout_s: float = 10.0) -> None:
        if len(token) < 32:
            raise ValueError("orchestrator token must be at least 32 bytes")
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, follow_redirects=False,
                                  headers={"authorization": "Bearer " + token.decode()})

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        try:
            resp = self._http.post(f"/internal/runs/{run_id}/kill", json={"actor": actor, "reason": reason})
        except httpx.HTTPError:
            raise UpstreamUnavailable("orchestrator") from None
        if resp.status_code >= 400:
            raise UpstreamUnavailable("orchestrator")
        return str(resp.json().get("result", ""))


class AccountEraser:
    def __init__(self, db_url: str, killer: RunKiller, auth: AuthAdmin, *, wait_s: float = 20.0,
                 poll_s: float = 0.5, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._url, self._killer, self._auth = db_url, killer, auth
        self._wait_s, self._poll_s, self._sleep, self._clock = wait_s, poll_s, sleep, clock

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self._url, row_factory=dict_row)

    def _active(self, conn: psycopg.Connection[dict[str, Any]], user_id: str) -> list[str]:
        rows = conn.execute("select id from public.runs where owner_id = %s and status = any(%s)",
                            (user_id, list(ACTIVE))).fetchall()
        return [str(r["id"]) for r in rows]

    def erase(self, user_id: str) -> Erased:
        if user_id == TOMBSTONE_ID:
            raise ValueError("the tombstone profile cannot be erased")
        with self._connect() as conn:
            conn.execute("set local role service_role")
            active = self._active(conn, user_id)
        for run_id in active:
            self._killer.kill(run_id, user_id, "account_erased")
        deadline = self._clock() + self._wait_s
        while active:
            with self._connect() as conn:
                conn.execute("set local role service_role")
                active = self._active(conn, user_id)
            if not active or self._clock() >= deadline:
                break
            self._sleep(self._poll_s)
        result = self._reassign(user_id)
        self._auth.delete_user(user_id)                       # sessions and refresh tokens go with the user
        log.info("account erased kept=%d removed=%d", result.kept, result.removed)
        return result

    def _reassign(self, user_id: str) -> Erased:
        with self._connect() as conn:
            conn.execute("set local role service_role")
            # the same lock create_run_request takes: no run can be created while this transaction decides
            conn.execute("select pg_advisory_xact_lock(hashtextextended('run:' || %s::text, 0))", (user_id,))
            profile = conn.execute("select 1 from public.profiles where id = %s for update", (user_id,)).fetchone()
            if profile is None:
                return Erased(0, 0)                           # already erased: a retry after a partial failure
            if self._active(conn, user_id):
                raise RunsStillActive
            conn.execute("update public.profiles set erased_at = now() where id = %s", (user_id,))
            kept = conn.execute(
                "update public.runs r set owner_id = %s, title = null where r.owner_id = %s "
                "and r.visibility = 'public' and r.deleted_at is null "
                "and exists (select 1 from public.run_scores s where s.run_id = r.id and s.verified) returning r.id",
                (TOMBSTONE_ID, user_id)).fetchall()
            removed = conn.execute(
                "update public.runs set owner_id = %s, title = null, deleted_at = coalesce(deleted_at, now()) "
                "where owner_id = %s returning id", (TOMBSTONE_ID, user_id)).fetchall()
            # configs of deleted runs are blanked; kept runs keep theirs (the benchmark must be reproducible)
            conn.execute(
                "update public.run_configs c set config_json = '{}'::jsonb where c.owner_id = %s and not exists "
                "(select 1 from public.runs r where r.config_id = c.id and r.deleted_at is null)", (user_id,))
            conn.execute("update public.run_configs set owner_id = %s where owner_id = %s", (TOMBSTONE_ID, user_id))
            conn.execute("insert into public.audit_log (actor_id, action, target) values (null, 'account.erased', null)")
            return Erased(len(kept), len(removed))
