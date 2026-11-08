"""Settings from the environment. Nothing secret lives here: the publishable key is public by design."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    api_url: str
    auth_url: str
    publishable_key: str
    web_url: str
    config_dir: Path

    @property
    def session_path(self) -> Path:
        return self.config_dir / "session.json"

    @property
    def api_base(self) -> str:
        return self.api_url.rstrip("/") + "/api/v1"

    @property
    def ws_base(self) -> str:
        base = self.api_url.rstrip("/")
        return ("wss://" + base[len("https://"):]) if base.startswith("https://") else ("ws://" + base.removeprefix("http://"))


def from_env(env: dict[str, str] | None = None) -> Config:
    e = dict(os.environ) if env is None else env
    api = e.get("MEMORYLAB_API_URL", "http://127.0.0.1:8000")
    return Config(
        api_url=api,
        auth_url=e.get("MEMORYLAB_AUTH_URL", "http://127.0.0.1:54321/auth/v1"),
        publishable_key=e.get("MEMORYLAB_PUBLISHABLE_KEY", ""),
        web_url=e.get("MEMORYLAB_WEB_URL", "http://localhost:3000"),
        config_dir=Path(e.get("MEMORYLAB_CONFIG_DIR") or (Path.home() / ".config" / "memorylab")))
