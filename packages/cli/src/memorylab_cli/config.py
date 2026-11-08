"""Settings from the environment. Nothing secret lives here: the publishable key is public by design."""

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import CliError


@dataclass(frozen=True)
class Config:
    api_url: str
    auth_url: str
    publishable_key: str
    web_url: str
    config_dir: Path
    warnings: tuple[str, ...] = field(default=(), compare=False)

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
    auth = e.get("MEMORYLAB_AUTH_URL", "http://127.0.0.1:54321/auth/v1")
    warnings = check_transport({"MEMORYLAB_API_URL": api, "MEMORYLAB_AUTH_URL": auth}, e.get("MEMORYLAB_ALLOW_INSECURE") == "1")
    return Config(
        api_url=api, auth_url=auth,
        publishable_key=e.get("MEMORYLAB_PUBLISHABLE_KEY", ""),
        web_url=e.get("MEMORYLAB_WEB_URL", "http://localhost:3000"),
        config_dir=Path(e.get("MEMORYLAB_CONFIG_DIR") or (Path.home() / ".config" / "memorylab")), warnings=warnings)


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_transport(urls: dict[str, str], allow_insecure: bool) -> tuple[str, ...]:
    """Passwords, tokens and provider keys must not cross a network in clear text: http:// is only for a loopback host
    (local development). MEMORYLAB_ALLOW_INSECURE=1 overrides that for development, loudly."""
    warnings: list[str] = []
    for name, url in urls.items():
        parts = urlparse(url)
        if parts.scheme == "https":
            continue
        if parts.scheme != "http" or not parts.hostname:
            raise CliError("config", f"{name} must be an https:// URL (got {parts.scheme or 'no'} scheme)")
        if _is_loopback(parts.hostname):
            continue
        if not allow_insecure:
            raise CliError("config", f"refusing {name} over plain http to {parts.hostname}: passwords and keys would cross the "
                           "network unencrypted. Use https://, or set MEMORYLAB_ALLOW_INSECURE=1 for development only")
        warnings.append(f"WARNING: {name} uses plain http to {parts.hostname}; credentials are sent unencrypted")
    return tuple(warnings)
