"""Source hash of what goes into the sandbox worker image (labelled memorylab.src by infra/docker/build.sh).

The orchestrator Docker tests recompute it and fail when the image label differs, so a stale image cannot pass the gate.
Usage: python scripts/image_src_hash.py   (prints the hex digest)
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("packages/labharness/src", "packages/runlog/src", "services/worker/src")
EXTRA = ("packages/labharness/pyproject.toml", "packages/runlog/pyproject.toml", "services/worker/pyproject.toml",
         "infra/docker/Dockerfile.worker")


def source_hash(root: Path = ROOT) -> str:
    files = [p for d in SOURCES for p in (root / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files += [root / e for e in EXTRA]
    h = hashlib.sha256()
    for p in sorted(files, key=lambda f: f.relative_to(root).as_posix()):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


if __name__ == "__main__":
    print(source_hash())
