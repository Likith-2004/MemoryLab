"""The stored session: access and refresh token plus who they belong to. Mode 0600 in a 0700 directory, written
atomically; never contains a provider key."""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Session:
    access_token: str
    refresh_token: str
    expires_at: float
    user_id: str
    email: str

    def expired(self, now: float, skew_s: float = 60.0) -> bool:
        return now + skew_s >= self.expires_at


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Session | None:
        try:
            raw = json.loads(self.path.read_text())
            return Session(str(raw["access_token"]), str(raw["refresh_token"]), float(raw["expires_at"]),
                           str(raw["user_id"]), str(raw["email"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, session: Session) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".session-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(session), f)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def from_token_response(body: dict[str, object], now: float | None = None) -> Session:
    now = time.time() if now is None else now
    user = body.get("user")
    user_d = user if isinstance(user, dict) else {}
    return Session(access_token=str(body["access_token"]), refresh_token=str(body["refresh_token"]),
                   expires_at=now + float(body.get("expires_in", 900)),     # type: ignore[arg-type]
                   user_id=str(user_d.get("id", "")), email=str(user_d.get("email", "")))
