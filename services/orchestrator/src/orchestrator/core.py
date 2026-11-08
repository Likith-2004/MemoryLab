"""The orchestrator: admits runs, queues them (keys held in memory only), starts one hardened
container per run, feeds it the job on stdin, supervises it, and finalizes the run's state."""

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from labharness import load_bundle
from labharness.agent import RunConfig
from labharness.constants import PINNED_PYBOY
from labharness.emulator import Limits
from labharness.presets import custom_preset, get_preset
from labharness.providers.registry import check_model
from labharness.providers.secret import SecretKey
from runlog.lifecycle import FINAL
from runlog.manifest import build_manifest
from runlog.redaction import ScrubFilter

from . import dockercmd
from .config import OrchestratorConfig
from .ingest_api import IngestApi
from .keybox import KeyBox
from .process import PopenProcess, ProcessLike
from .sandbox import Sandbox, SandboxResult
from .scheduler import Duplicate, RunRequest, Scheduler

log = logging.getLogger("orchestrator")
log.addFilter(ScrubFilter())


class SubmitError(Exception):
    code = "validation_failed"
    status = 422

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


class DockerOps(Protocol):
    def check_network(self) -> None: ...
    def kill(self, run_id: str) -> None: ...
    def remove(self, run_id: str) -> None: ...


class RealDocker:
    def __init__(self, cfg: OrchestratorConfig) -> None:
        self.cfg = cfg

    def check_network(self) -> None:
        dockercmd.require_internal_network(self.cfg)

    def kill(self, run_id: str) -> None:
        dockercmd.kill_container(self.cfg, run_id)

    def remove(self, run_id: str) -> None:
        dockercmd.remove_container(self.cfg, run_id)


@dataclass(frozen=True)
class Submission:
    run_id: str
    user_id: str
    ip_hash: str
    game: str
    run_config: dict[str, Any]
    preset_text: str | None
    provider: dict[str, Any]        # {"name": "anthropic", "api_key": "..."} (or fake in tests)


class Orchestrator:
    def __init__(self, cfg: OrchestratorConfig, http: httpx.Client, *,
                 launcher: Callable[[list[str]], ProcessLike] = PopenProcess,
                 docker: DockerOps | None = None, clock: Callable[[], float] = time.monotonic,
                 audit: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.cfg, self._http, self._launcher, self._clock = cfg, http, launcher, clock
        self._docker: DockerOps = docker or RealDocker(cfg)
        self._audit = audit or (lambda _event: None)
        self.scheduler = Scheduler(cfg, clock)
        self._sandboxes: dict[str, Sandbox] = {}
        self._apis: dict[str, IngestApi] = {}
        self._threads: list[threading.Thread] = []
        self._stop_loop = threading.Event()
        self._lock = threading.Lock()

    def _api(self, run_id: str) -> IngestApi:
        """One ingest handle per run, so batch numbering stays consistent across calls."""
        with self._lock:
            if run_id not in self._apis:
                self._apis[run_id] = IngestApi(self._http, run_id, self.cfg.token_secret)
            return self._apis[run_id]

    # ---- admission -------------------------------------------------------------------------------
    def _validate(self, sub: Submission) -> tuple[RunConfig, str | None, SecretKey | None, list[dict[str, Any]] | None]:
        try:
            config = RunConfig.from_dict(sub.run_config)
        except (ValueError, TypeError) as exc:
            raise SubmitError("validation_failed", f"run_config: {str(exc)[:160]}") from None
        lim, caps = self.cfg.limits, config.caps
        over = [name for name, value, cap in (
            ("max_steps", caps.max_steps, lim.max_steps), ("max_tokens", caps.max_tokens, lim.max_tokens),
            ("max_wall_s", caps.max_wall_s, lim.max_wall_s), ("max_output_tokens", caps.max_output_tokens, lim.max_output_tokens),
            ("max_tool_calls_per_step", caps.max_tool_calls_per_step, lim.max_tool_calls_per_step),
            ("context_steps", caps.context_steps, lim.max_context_steps)) if value > cap]
        if over:
            raise SubmitError("caps_exceed_limit", f"over the server maximum: {', '.join(over)}")
        prov = sub.provider
        name = prov.get("name")
        script = None
        if name == "fake" and self.cfg.allow_fake_provider:
            script = prov.get("script", [])
        else:
            try:
                check_model(str(name), config.model_id)
            except ValueError as exc:
                raise SubmitError("validation_failed", str(exc)) from None
            if config.provider != name:
                raise SubmitError("validation_failed", "provider mismatch")
        key = None
        if prov.get("api_key") is not None:
            try:
                key = SecretKey(prov["api_key"])
            except ValueError:
                raise SubmitError("key_rejected", "the key has an invalid shape") from None
        elif name != "fake":
            raise SubmitError("key_rejected", "an API key is required")
        is_custom = config.preset_ref.startswith("custom@")
        if is_custom and (config.mode != "demo" or not sub.preset_text):
            raise SubmitError("validation_failed", "custom instructions are Demo mode only and need text")
        if not is_custom:
            if sub.preset_text is not None:
                raise SubmitError("validation_failed", "preset_text is only for custom instructions")
            try:
                get_preset(config.preset_ref)
            except ValueError:
                raise SubmitError("validation_failed", "unknown preset") from None
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", sub.game) or not (Path(self.cfg.games_dir) / sub.game / "bundle.json").is_file():
            raise SubmitError("validation_failed", "unknown game")
        return config, sub.preset_text, key, script

    def submit(self, sub: Submission) -> dict[str, Any]:
        """Validate and queue. Raises SubmitError (422) or AdmissionError (429/503/409). The key is
        held in a KeyBox: memory only, never written anywhere. Idempotent per run_id: a repeat returns the
        current status and queues nothing."""
        config, preset_text, key, script = self._validate(sub)
        box = KeyBox(key) if key is not None else KeyBox(SecretKey("placeholder-unused-key"))
        if key is None:
            box.discard()
        req = RunRequest(sub.run_id, sub.user_id, sub.ip_hash, sub.game, config, preset_text,
                         str(sub.provider["name"]), box, script, self._clock())
        try:
            position = self.scheduler.submit(req)
        except Duplicate:
            # idempotent per run_id: the API retries an ambiguous submit, and the first attempt may have landed.
            # Only for the same user: another user naming an existing run id is still a conflict.
            box.discard()
            if self.scheduler.user_of(sub.run_id) != sub.user_id:
                raise
            return {"run_id": sub.run_id, "status": self.scheduler.status_of(sub.run_id), "duplicate": True}
        try:
            self._api(sub.run_id).state("queued")
        except Exception:
            self.scheduler.cancel_queued(sub.run_id)
            self.scheduler.finished(sub.run_id)
            with self._lock:
                self._apis.pop(sub.run_id, None)
            raise
        return {"run_id": sub.run_id, "status": "queued", "position": position}

    # ---- scheduling ------------------------------------------------------------------------------------
    def tick(self) -> None:
        for expired in self.scheduler.expire():
            self._finish_unstarted(expired.run_id, "failed", "key_expired")
        while (ready := self.scheduler.next_ready()) is not None:
            thread = threading.Thread(target=self._run, args=(ready,), daemon=True, name=f"run-{ready.run_id[:8]}")
            with self._lock:
                self._threads.append(thread)
            thread.start()

    def start(self, period_s: float = 1.0) -> None:
        def loop() -> None:
            while not self._stop_loop.wait(period_s):
                self.tick()
        threading.Thread(target=loop, daemon=True, name="orchestrator-tick").start()

    def shutdown(self) -> None:
        self._stop_loop.set()

    def wait_idle(self, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive and not self.scheduler.snapshot()["queued"]:
                return True
            time.sleep(0.05)
        return False

    # ---- one run -----------------------------------------------------------------------------------------------
    def _run(self, req: RunRequest) -> None:
        api = self._api(req.run_id)
        result: SandboxResult | None = None
        try:
            self._docker.check_network()
            api.state("starting")
            game = load_bundle(Path(self.cfg.games_dir) / req.game)
            config: RunConfig = req.run_config
            preset = custom_preset(req.preset_text or "") if config.preset_ref.startswith("custom@") else get_preset(config.preset_ref)
            manifest = build_manifest(run_id=req.run_id, config=config, bundle=game, preset=preset,
                                      pyboy_version=PINNED_PYBOY, max_total_frames=Limits().max_total_frames)
            api.put_manifest(manifest)                                   # sealed before step 1
            argv = dockercmd.build_run_command(self.cfg, req.run_id)
            proc = self._launcher(argv)
            sandbox = Sandbox(proc, self.cfg, wall_limit_s=config.caps.max_wall_s,
                              kill_container=lambda: self._docker.kill(req.run_id))
            with self._lock:
                self._sandboxes[req.run_id] = sandbox
            sandbox.send_job(self._job_line(req, api, config))
            result = sandbox.run()
            self._finalize(api, req, result)
        except Exception as exc:  # noqa: BLE001 - a failed run must end up `failed`, never stuck
            log.error("run %s failed in the orchestrator: %s", req.run_id, type(exc).__name__)
            self._fail_quietly(api, type(exc).__name__)
        finally:
            req.key_box.discard()
            try:
                self._docker.remove(req.run_id)
            except Exception:  # noqa: BLE001
                log.error("could not remove container for %s", req.run_id)
            with self._lock:
                self._sandboxes.pop(req.run_id, None)
                self._apis.pop(req.run_id, None)
            self.scheduler.finished(req.run_id)

    def _job_line(self, req: RunRequest, api: IngestApi, config: RunConfig) -> bytes:
        """Build the job spec. The key is taken from its box exactly once, serialised straight into
        the bytes written to stdin, and not kept anywhere else."""
        caps = config.caps
        provider: dict[str, Any]
        if req.provider_name == "fake":
            provider = {"name": "fake", "script": req.fake_script or []}
            if not req.key_box.empty:
                provider["api_key"] = req.key_box.take().reveal()
        else:
            provider = {"name": req.provider_name, "api_key": req.key_box.take().reveal()}
        job = {"v": 1, "run_id": req.run_id, "game": req.game, "run_config": config.to_dict(),
               "preset_text": req.preset_text,
               "ingest": {"url": self.cfg.sandbox_ingest_url, "proxy": self.cfg.proxy_url,
                          "token": api.worker_token(int(caps.max_wall_s + self.cfg.wall_grace_s + 600))},
               "provider": provider, "heartbeat_s": self.cfg.heartbeat_period_s}
        return json.dumps(job, separators=(",", ":")).encode()

    def _finalize(self, api: IngestApi, req: RunRequest, result: SandboxResult) -> None:
        status = api.status()["status"]
        if status not in FINAL:                                          # the worker did not finish the run
            if result.killed_reason == "admin_kill":
                api.state("killed", "admin_kill")
            else:
                reason = result.killed_reason or result.fatal or f"worker_exit_{result.exit_code}"
                api.state("failed", reason)
        api.close()

    def _fail_quietly(self, api: IngestApi, reason: str) -> None:
        try:
            if api.status()["status"] not in FINAL:
                api.state("failed", reason)
        except Exception:  # noqa: BLE001 - the ingest service itself may be the problem
            log.error("could not record failure for %s", api.run_id)

    def _finish_unstarted(self, run_id: str, state: str, reason: str) -> None:
        try:
            self._api(run_id).state(state, reason)
        except Exception:  # noqa: BLE001
            log.error("could not record %s for %s", state, run_id)
        self.scheduler.finished(run_id)
        with self._lock:
            self._apis.pop(run_id, None)

    # ---- controls ------------------------------------------------------------------------------------------------
    def stop(self, run_id: str, actor: str) -> str:
        """Graceful stop. Returns what happened: stopping | cancelled | unknown."""
        sandbox = self._sandboxes.get(run_id)
        if sandbox is not None:
            sandbox.stop()
            self._audit({"action": "stop", "actor": actor, "target": run_id})
            return "stopping"
        queued = self.scheduler.cancel_queued(run_id)
        if queued is not None:
            self._finish_unstarted(run_id, "killed", "user_cancelled")
            return "cancelled"
        return "unknown"

    def kill(self, run_id: str, actor: str, reason: str) -> str:
        if not reason.strip():
            raise ValueError("a reason is required")
        self._audit({"action": "kill", "actor": actor, "target": run_id, "reason": reason})
        sandbox = self._sandboxes.get(run_id)
        if sandbox is not None:
            sandbox.kill("admin_kill")
            return "killed"
        queued = self.scheduler.cancel_queued(run_id)
        if queued is not None:
            self._finish_unstarted(run_id, "killed", "admin_kill")
            return "killed"
        return "unknown"

    def set_breaker(self, *, open_: bool, reason: str, actor: str) -> dict[str, Any]:
        state = self.scheduler.set_breaker(open_=open_, reason=reason, actor=actor)
        self._audit({"action": "breaker", "actor": actor, "open": open_, "reason": reason})
        return {"open": state.open, "reason": state.reason}
