"""Ingest business logic: authenticate, validate, redact, append, index, quota, close."""

import hashlib
import io
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PIL import Image, UnidentifiedImageError
from runlog.bundle import ExportRefused, build_export
from runlog.eventlog import EventLogReader, EventLogWriter
from runlog.events import MEM_DELETE_TYPES, MEM_WRITE_TYPES, EventRejected, validate_payload
from runlog.lifecycle import FINAL, check_transition
from runlog.manifest import ManifestSealed, seal_manifest
from runlog.redaction import Redactor, scan_text
from runlog.store import AlreadyExists, NotFound, ObjectStore

from .repository import ParentRef, RunRecord, RunRepository
from .tokens import Claims, TokenError, verify_token

ORCHESTRATOR_TYPES = frozenset({"run.state", "error"})
FRAME_MAX_DIM = (640, 576)


class IngestError(Exception):
    """Base for errors mapped to HTTP problems. `code` is stable; `status` is the HTTP status."""

    status = 400
    code = "bad_request"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class Unauthenticated(IngestError):
    status, code = 401, "unauthenticated"


class WrongRun(IngestError):
    status, code = 403, "forbidden_run"


class WrongScope(IngestError):
    status, code = 403, "forbidden_scope"


class UnknownRun(IngestError):
    status, code = 404, "not_found"


class Conflict(IngestError):
    status, code = 409, "conflict"


class Invalid(IngestError):
    status, code = 422, "validation_failed"


class QuotaExceeded(IngestError):
    status, code = 429, "quota_exceeded"


@dataclass(frozen=True)
class Limits:
    max_batch_events: int = 100
    max_events_per_run: int = 200_000
    max_bytes_per_run: int = 256 * 1024 * 1024
    max_frames_per_run: int = 3000
    max_frame_bytes: int = 256 * 1024
    max_manifest_bytes: int = 64 * 1024
    max_body_bytes: int = 4 * 1024 * 1024
    max_checkpoints_per_run: int = 200
    max_checkpoint_state_bytes: int = 3 * 1024 * 1024
    max_checkpoint_context_bytes: int = 1024 * 1024
    max_parent_payload_bytes: int = 24 * 1024 * 1024


class IngestService:
    def __init__(self, repo: RunRepository, store: ObjectStore, secret: bytes, *, limits: Limits | None = None,
                 clock: Callable[[], float] = time.time, redactor: Redactor | None = None) -> None:
        self.repo, self.store, self._secret = repo, store, secret
        self.limits = limits or Limits()
        self.clock = clock
        self.redactor = redactor or Redactor()  # pattern layer: ingest does not know visitor keys
        self._writers: dict[str, EventLogWriter] = {}
        self._writers_guard = threading.Lock()

    # ---- auth ------------------------------------------------------------------------------
    def authenticate(self, token: object, run_id: str) -> Claims:
        try:
            claims = verify_token(self._secret, token, self.clock())
        except TokenError:
            raise Unauthenticated("invalid or expired token") from None
        if claims.run_id != run_id:
            raise WrongRun("token is not valid for this run")
        return claims

    def _record(self, run_id: str) -> RunRecord:
        record = self.repo.get(run_id)
        if record is None:
            raise UnknownRun("no such run")
        return record

    def _writer(self, run_id: str) -> EventLogWriter:
        with self._writers_guard:
            if run_id not in self._writers:
                self._writers[run_id] = EventLogWriter(self.store, run_id)
            return self._writers[run_id]

    # ---- events ------------------------------------------------------------------------------
    def append(self, claims: Claims, run_id: str, batch_no: int, events: list[dict[str, Any]]) -> dict[str, Any]:
        if not 1 <= len(events) <= self.limits.max_batch_events:
            raise Invalid(f"a batch holds 1-{self.limits.max_batch_events} events")
        with self.repo.lock(run_id):
            record = self._record(run_id)
            if record.closed:
                raise Conflict("the run is closed")
            expected = record.next_batch_no[claims.scope]  # worker and orchestrator number their batches separately
            if batch_no == expected - 1 and claims.scope in record.last_ack:
                return record.last_ack[claims.scope]  # a retry of the batch we already took: acknowledge, never duplicate
            if batch_no != expected:
                raise Conflict(f"expected batch {expected}")
            if record.status in FINAL:
                raise Conflict("the run has ended")
            if record.event_count + len(events) > self.limits.max_events_per_run:
                raise QuotaExceeded("event quota for this run is used up")
            status = record.status
            items: list[tuple[str, int, dict[str, Any]]] = []
            for ev in events:
                type_, step, data = ev.get("type"), ev.get("step"), ev.get("data")
                if status in FINAL:
                    raise Invalid("no events may follow the final state")
                try:
                    validate_payload(type_, step, data)
                    if claims.scope == "orchestrator" and type_ not in ORCHESTRATOR_TYPES:
                        raise WrongScope(f"the orchestrator may not write {str(type_)[:20]} events")
                    assert isinstance(data, dict) and isinstance(type_, str) and isinstance(step, int)
                    if type_ == "run.state":
                        status = check_transition(status, data.get("state"), claims.scope)
                    elif type_ == "checkpoint":
                        self._check_checkpoint_event(run_id, data)
                except EventRejected as exc:
                    raise Invalid(exc.reason) from None
                items.append((type_, step, self.redactor.obj(data)))
            size = sum(len(json.dumps(data)) for _t, _s, data in items)
            if record.bytes_total + size > self.limits.max_bytes_per_run:
                raise QuotaExceeded("byte quota for this run is used up")
            written, index = self._writer(run_id).append(items)
            for e in written:
                if e.type in ("model.call", "dream.call"):          # the dream job's spend counts against the run's total
                    record.tokens = int(e.data.get("tokens_total", record.tokens))
                elif e.type == "obs":
                    record.steps = max(record.steps, e.step)
                    record.score = int(e.data.get("score", record.score))
            self.repo.add_index(run_id, index)
            for e in written:
                if e.type == "checkpoint":      # a row per checkpoint: forks reference it; its offset is this event's seq
                    self.repo.add_checkpoint(run_id, {"step": e.step, "savestate_key": self._cp_key(run_id, e.step, "state"),
                                                      "memory_version": int(e.data["memory_rev"]), "transcript_offset": e.seq,
                                                      "config_hash": str(e.data["config_hash"])})
            rows = self._memory_rows(run_id, written)
            if rows:
                self.repo.add_memory_rows(run_id, rows)
            record.status = status
            record.event_count += len(written)
            record.bytes_total += size
            record.next_batch_no[claims.scope] = expected + 1
            ack = {"batch_no": batch_no, "accepted": len(written), "next_seq": written[-1].seq + 1}
            record.last_ack[claims.scope] = ack
            self.repo.save(record)
            return ack

    @staticmethod
    def _memory_rows(run_id: str, events: list[Any]) -> list[dict[str, Any]]:
        rows = []
        for e in events:
            if e.type in MEM_WRITE_TYPES:                           # the agent's writes and the dream job's: one versioned store
                d = e.data
                rows.append({"run_id": run_id, "path": d["path"], "version": d["version"], "step": d["step"],
                             "rev": d["rev"], "content": d["content"], "chars": d["chars"], "deleted": False})
            elif e.type in MEM_DELETE_TYPES:
                d = e.data
                rows.append({"run_id": run_id, "path": d["path"], "version": d["version"], "step": d["step"],
                             "rev": d["rev"], "content": "", "chars": 0, "deleted": True})
        return rows

    # ---- checkpoints and forks -------------------------------------------------------------------
    @staticmethod
    def _cp_key(run_id: str, step: int, kind: str) -> str:
        return f"runs/{run_id}/checkpoints/{step:07d}.{'state' if kind == 'state' else 'ctx.json'}"

    def put_checkpoint_blob(self, claims: Claims, run_id: str, step: int, kind: str, data: bytes) -> None:
        """The worker stores a checkpoint's emulator state and model context BEFORE it logs the `checkpoint` event that announces them."""
        if claims.scope != "worker":
            raise WrongScope("only the worker uploads checkpoints")
        limit = self.limits.max_checkpoint_state_bytes if kind == "state" else self.limits.max_checkpoint_context_bytes
        if kind not in ("state", "context") or not 0 <= step <= 1_000_000 or not 0 < len(data) <= limit:
            raise Invalid("bad checkpoint step, kind or size")
        if kind == "context":
            try:
                json.loads(data)
            except ValueError:
                raise Invalid("a checkpoint context is JSON") from None
            if scan_text(data.decode("utf-8", "replace")):
                raise Invalid("checkpoint context contains credential-like text")     # the same refusal as a manifest
        with self.repo.lock(run_id):
            record = self._record(run_id)
            if record.closed or record.status in FINAL:
                raise Conflict("the run has ended")
            key = self._cp_key(run_id, step, kind)
            known_step = self.store.exists(self._cp_key(run_id, step, "state")) or self.store.exists(self._cp_key(run_id, step, "context"))
            if not known_step and record.checkpoints >= self.limits.max_checkpoints_per_run:
                raise QuotaExceeded("checkpoint quota for this run is used up")      # counted per distinct step, at upload, orphans included
            try:
                self.store.put(key, data)
            except AlreadyExists:
                if self.store.get(key) != data:
                    raise Conflict("a different checkpoint already exists for this step") from None
                return                                                              # an idempotent re-upload costs nothing
            if not known_step:
                record.checkpoints += 1
                self.repo.save(record)

    def _check_checkpoint_event(self, run_id: str, data: dict[str, Any]) -> None:
        step, sha = data.get("step"), data.get("state_sha256")
        if isinstance(step, bool) or not isinstance(step, int) or not isinstance(sha, str) or not isinstance(data.get("config_hash"), str) \
                or isinstance(data.get("memory_rev"), bool) or not isinstance(data.get("memory_rev"), int):
            raise Invalid("malformed checkpoint event")
        try:
            blob = self.store.get(self._cp_key(run_id, step, "state"))
            self.store.get(self._cp_key(run_id, step, "context"))
        except NotFound:
            raise Invalid("a checkpoint event needs its uploaded state and context") from None
        if hashlib.sha256(blob).hexdigest() != sha:
            raise Invalid("the checkpoint state does not match its hash")

    def _parent(self, claims: Claims, run_id: str) -> ParentRef:
        if claims.scope != "worker":
            raise WrongScope("only the worker reads its parent checkpoint")
        ref = self.repo.parent_of(run_id)
        if ref is None:
            raise UnknownRun("this run has no parent checkpoint")
        return ref

    def parent_payload(self, claims: Claims, run_id: str) -> dict[str, Any]:
        """What a forked run starts from: the parent's `checkpoint` event, its model context, and its memory mutations up to that point."""
        ref = self._parent(claims, run_id)
        checkpoint: dict[str, Any] | None = None
        memory: list[dict[str, Any]] = []
        size = 0
        for e in EventLogReader(self.store, ref.parent_run_id).iter_events():
            if e.seq > ref.transcript_offset:
                break
            if e.type in MEM_WRITE_TYPES | MEM_DELETE_TYPES:
                memory.append({"type": e.type, "step": e.step, "data": e.data})
                size += len(json.dumps(e.data))
                if size > self.limits.max_parent_payload_bytes:
                    raise Invalid("the parent's memory history is too large to fork")
            elif e.seq == ref.transcript_offset and e.type == "checkpoint":
                checkpoint = e.data
        if checkpoint is None or checkpoint.get("step") != ref.step:
            raise UnknownRun("the parent's checkpoint is not in its log")
        try:
            context = json.loads(self.store.get(self._cp_key(ref.parent_run_id, ref.step, "context")))
        except NotFound:
            raise UnknownRun("the parent's checkpoint context is missing") from None
        return {"parent_run_id": ref.parent_run_id, "checkpoint": checkpoint, "context": context, "memory_events": memory}

    def parent_state(self, claims: Claims, run_id: str) -> bytes:
        ref = self._parent(claims, run_id)
        try:
            return self.store.get(self._cp_key(ref.parent_run_id, ref.step, "state"))
        except NotFound:
            raise UnknownRun("the parent's checkpoint state is missing") from None

    # ---- frames, manifest, close -----------------------------------------------------------------
    def put_frame(self, claims: Claims, run_id: str, step: int, png: bytes) -> None:
        if claims.scope != "worker":
            raise WrongScope("only the worker uploads frames")
        if not 0 <= step <= 1_000_000 or not 0 < len(png) <= self.limits.max_frame_bytes:
            raise Invalid("bad frame step or size")
        try:
            image = Image.open(io.BytesIO(png))
            image.verify()
            fmt, size, mode = image.format, image.size, image.mode
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
            raise Invalid("not a valid PNG") from None
        if fmt != "PNG" or mode != "RGB" or not (0 < size[0] <= FRAME_MAX_DIM[0] and 0 < size[1] <= FRAME_MAX_DIM[1]):
            raise Invalid("frame must be an RGB PNG within the size limit")
        with self.repo.lock(run_id):
            record = self._record(run_id)
            if record.closed:
                raise Conflict("the run is closed")
            if record.frames >= self.limits.max_frames_per_run:
                raise QuotaExceeded("frame quota for this run is used up")
            key = f"runs/{run_id}/frames/{step:07d}.png"
            try:
                self.store.put(key, png)
            except AlreadyExists:
                if self.store.get(key) != png:
                    raise Conflict("a different frame already exists for this step") from None
                return
            record.frames += 1
            self.repo.save(record)

    def put_manifest(self, claims: Claims, run_id: str, manifest: dict[str, Any]) -> None:
        if claims.scope != "orchestrator":
            raise WrongScope("only the orchestrator seals manifests")
        raw = json.dumps(manifest, sort_keys=True)
        if len(raw) > self.limits.max_manifest_bytes or manifest.get("run_id") != run_id or scan_text(raw):
            raise Invalid("manifest rejected")
        with self.repo.lock(run_id):
            record = self._record(run_id)
            try:
                seal_manifest(self.store, run_id, manifest)
            except ManifestSealed:
                raise Conflict("the manifest is already sealed") from None
            record.manifest_sealed = True
            self.repo.save(record)

    def run_status(self, claims: Claims, run_id: str) -> dict[str, Any]:
        """For the orchestrator: where the run stands, so it can finalize runs whose worker died."""
        if claims.scope != "orchestrator":
            raise WrongScope("only the orchestrator reads run status")
        record = self._record(run_id)
        return {"status": record.status, "closed": record.closed, "events": record.event_count,
                "manifest_sealed": record.manifest_sealed, "next_batch_no": record.next_batch_no["orchestrator"]}

    def close_run(self, claims: Claims, run_id: str) -> dict[str, Any]:
        if claims.scope != "orchestrator":
            raise WrongScope("only the orchestrator closes runs")
        with self.repo.lock(run_id):
            record = self._record(run_id)
            if record.closed:
                return {"closed": True, "bundle_sha256": record.bundle_sha256}
            if record.status not in FINAL:
                raise Conflict("the run has not ended")
            if not record.manifest_sealed:
                raise Conflict("the run has no sealed manifest")
            self._writer(run_id).close()
            try:
                info = build_export(self.store, run_id)
            except ExportRefused:
                raise Conflict("export refused: the run contains credential-like text") from None
            record.closed = True
            record.bundle_sha256 = info.sha256
            self.repo.save(record)
            return {"closed": True, "bundle_sha256": info.sha256}
