"""What the dream job changed, from the run's event log: each change with the file as it was just before it."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DreamChange:
    session: int
    step: int
    path: str
    kind: str                # create | correct | promote | prune
    version: int
    before: str | None       # None: the file did not exist
    after: str | None        # None: the dream deleted it

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def dream_changes(events: list[dict[str, Any]], at_step: int | None = None) -> list[DreamChange]:
    live: dict[str, str] = {}
    out: list[DreamChange] = []
    session = 0
    for e in events:
        if at_step is not None and int(e["step"]) > at_step:
            break
        d, kind = e.get("data", {}), str(e.get("type"))
        path = d.get("path")
        if kind == "dream.start":
            session = int(d.get("session", session + 1))
        if not isinstance(path, str) or kind not in ("mem.write", "mem.delete", "dream.write", "dream.delete"):
            continue
        deleted = kind.endswith("delete")
        if kind.startswith("dream."):
            out.append(DreamChange(session, int(e["step"]), path, str(d.get("kind", "prune" if deleted else "correct")),
                                   int(d.get("version", 0)), live.get(path), None if deleted else str(d.get("content", ""))))
        if deleted:
            live.pop(path, None)
        else:
            live[path] = str(d.get("content", ""))
    return out
