"""Export bundle: a self-contained zip anyone can re-check.

Contents are fixed names only (no user-controlled file names, so extraction cannot write
outside a target directory), built deterministically (fixed timestamps, sorted entries) so the
same run always yields the same sha256, and scanned for key patterns before it is written.
"""

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from labharness.constants import canonical_json

from .eventlog import EventLogReader, load_index
from .events import Event, EventRejected, canonical, parse_line
from .manifest import load_manifest, manifest_sha256
from .redaction import scan_bytes
from .replay import compute_score, memory_rows, memory_tree_at, verify_detectors, verify_log
from .store import AlreadyExists, ObjectStore

FORMAT = 1
_FRAME = re.compile(r"frames/\d{7}\.png")
_FIXED = frozenset({"bundle.json", "manifest.json", "events.jsonl", "memory/final.json", "memory/history.jsonl", "score.json"})
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024


class ExportRefused(Exception):
    """The bundle would contain something that looks like a credential."""


@dataclass(frozen=True)
class BundleInfo:
    sha256: str
    size: int
    files: dict[str, dict[str, Any]]


@dataclass
class BundleReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def _frames(store: ObjectStore, run_id: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    prefix = f"runs/{run_id}/frames/"
    for key in store.list(prefix):
        name = "frames/" + key[len(prefix):]
        if _FRAME.fullmatch(name):
            out[name] = store.get(key)
    return out


def build_export(store: ObjectStore, run_id: str) -> BundleInfo:
    """Build runs/{run_id}/bundle.zip for a closed run (idempotent: returns the existing one)."""
    key = f"runs/{run_id}/bundle.zip"
    if store.exists(key):
        data = store.get(key)
        existing = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("bundle.json"))["files"]
        return BundleInfo(hashlib.sha256(data).hexdigest(), len(data), existing)
    if not load_index(store, run_id).get("closed"):
        raise ValueError("the run's event log is not closed yet")
    manifest = load_manifest(store, run_id)
    events: list[Event] = list(EventLogReader(store, run_id).iter_events())
    files: dict[str, bytes] = {
        "manifest.json": canonical_json(manifest),
        "events.jsonl": b"".join((canonical(e.to_obj()) + "\n").encode() for e in events),
        "memory/final.json": canonical_json(memory_tree_at(events)),
        "memory/history.jsonl": b"".join((canonical(r) + "\n").encode() for r in memory_rows(events)),
        "score.json": canonical_json(compute_score(events)),
    }
    files.update(_frames(store, run_id))
    listing = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in sorted(files.items())}
    files["bundle.json"] = canonical_json({
        "format": FORMAT, "run_id": run_id, "harness_version": manifest["harness_version"],
        "manifest_sha256": manifest_sha256(manifest), "files": listing,
        "verify": "python -m runlog verify bundle.zip [--game <game bundle dir>]",
    })
    for name, data in files.items():  # the redaction scan re-runs at build time
        hits = scan_bytes(data)
        if hits:
            raise ExportRefused(f"{name} contains credential-like text ({', '.join(hits)})")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])
    blob = buf.getvalue()
    try:
        store.put(key, blob)
    except AlreadyExists:
        blob = store.get(key)
    return BundleInfo(hashlib.sha256(blob).hexdigest(), len(blob), {k: dict(v) for k, v in listing.items()})


def verify_bundle(blob: bytes) -> BundleReport:
    """Check a bundle without extracting it: names, sizes, checksums, log integrity, detectors."""
    report = BundleReport(True)

    def fail(msg: str) -> BundleReport:
        report.ok = False
        report.problems.append(msg)
        return report

    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return fail("not a zip file")
    infos = zf.infolist()
    names = [i.filename for i in infos]
    if len(set(names)) != len(names):
        return fail("duplicate entries")
    for info in infos:
        if info.filename not in _FIXED and not _FRAME.fullmatch(info.filename):
            return fail(f"unexpected entry name {info.filename[:40]!r}")
        if info.file_size > MAX_FILE_BYTES:
            return fail(f"{info.filename} is too large")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        return fail("bundle expands beyond the size limit")
    if not _FIXED <= set(names):
        return fail("missing required entries")
    meta = json.loads(zf.read("bundle.json"))
    if meta.get("format") != FORMAT:
        return fail("unsupported bundle format")
    for name, listed in meta["files"].items():
        if name not in names:
            report.problems.append(f"{name} listed but missing")
        elif hashlib.sha256(zf.read(name)).hexdigest() != listed["sha256"]:
            report.problems.append(f"{name} failed its checksum")
    for name in names:
        if name != "bundle.json" and name not in meta["files"]:
            report.problems.append(f"{name} is not listed")
    try:
        manifest = json.loads(zf.read("manifest.json"))
        if manifest_sha256(manifest) != meta["manifest_sha256"]:
            report.problems.append("manifest hash does not match")
        events = [parse_line(line) for line in zf.read("events.jsonl").decode().splitlines()]
        report.problems += verify_log(events) + verify_detectors(events)
        if canonical_json(compute_score(events)) != zf.read("score.json"):
            report.problems.append("score.json does not match the events")
        report.summary = {"run_id": meta["run_id"], "events": len(events), **compute_score(events)}
    except (EventRejected, KeyError, TypeError, ValueError, AttributeError, UnicodeDecodeError) as exc:
        # An untrusted bundle can carry malformed data: that is a verification failure, not a crash.
        report.problems.append(f"malformed bundle content ({type(exc).__name__})")
    report.ok = not report.problems
    return report
