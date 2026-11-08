"""Postgres access with the caller's identity applied, so RLS is what authorizes reads.

Each request runs in one transaction under role `authenticated` (or `anon`) with the verified `sub`
in `request.jwt.claims` — exactly what PostgREST sets — so `auth.uid()` works in every policy.
The service role is not available here (it exists only in worker-ingest and admin services).
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .auth import User

Conn = Connection[dict[str, Any]]


class Database:
    def __init__(self, url: str, *, max_size: int = 8) -> None:
        self.pool: ConnectionPool[Conn] = ConnectionPool(
            url, min_size=1, max_size=max_size, open=True, kwargs={"row_factory": dict_row, "autocommit": False})

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def session(self, user: User | None) -> Iterator[Conn]:
        with self.pool.connection() as conn:
            self.apply_identity(conn, user)
            yield conn
            # leaving the pool context commits; `set local` dies with the transaction

    @staticmethod
    def apply_identity(conn: Conn, user: User | None) -> None:
        """Must be called again after any commit: `set local` ends with the transaction, and the pool's
        login role is not the caller."""
        claims = {"role": "anon"} if user is None else {"sub": user.id, "role": "authenticated"}
        conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        conn.execute("set local role anon" if user is None else "set local role authenticated")

    @staticmethod
    def is_admin(conn: Conn) -> bool:
        """Admin = profiles.role in the DB, never a JWT claim."""
        return bool(conn.execute("select public.is_admin() as v").fetchone()["v"])  # type: ignore[index]
