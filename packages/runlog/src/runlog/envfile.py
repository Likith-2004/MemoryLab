"""A secret from the environment, or from a file (a Docker/Compose secret mounted under /run/secrets).

`NAME` holds the value, or `NAME_FILE` holds the path of a file that does. Files are preferred in deployment: an environment variable is
shown by `docker inspect` and readable from /proc/<pid>/environ by anyone with Docker access on the host; a secret file is neither.
Setting both is an error (one of them would silently win)."""

import os
from collections.abc import Mapping


def secret_env(name: str, env: Mapping[str, str] | None = None, default: str | None = None) -> str:
    e = os.environ if env is None else env
    path = e.get(name + "_FILE")
    if path:
        if name in e:
            raise ValueError(f"set {name} or {name}_FILE, not both")
        with open(path, encoding="utf-8") as f:
            return f.read().rstrip("\r\n")
    if name in e:
        return e[name]
    if default is not None:
        return default
    raise KeyError(name)
