"""Run manifest: written before step 1, then sealed (write-once)."""

import hashlib
import json
from pathlib import Path
from typing import Any

import labharness
from labharness.agent import RunConfig
from labharness.bundle import GameBundle
from labharness.constants import canonical_json, semantics_sha256
from labharness.presets import Preset
from labharness.prompt import BASE_PROMPT_SHA256, BASE_PROMPT_VERSION

from .store import AlreadyExists, ObjectStore


class ManifestSealed(Exception):
    pass


def harness_source_sha256() -> str:
    """Hash of every harness source file: two runs are comparable only if this matches."""
    root = Path(labharness.__file__).parent
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def build_manifest(*, run_id: str, config: RunConfig, bundle: GameBundle, preset: Preset, pyboy_version: str,
                   max_total_frames: int, parent: dict[str, Any] | None = None,
                   sessions: dict[str, Any] | None = None) -> dict[str, Any]:
    c = config
    return {
        "run_id": run_id,
        "mode": c.mode,
        "harness_version": labharness.HARNESS_VERSION,
        "harness_sha": harness_source_sha256(),
        "semantics_sha256": semantics_sha256(bundle.release_frames),
        "pyboy_version": pyboy_version,
        "release_frames": bundle.release_frames,
        "game": {"id": bundle.game_id, "rom_sha256": bundle.rom_info.sha256,
                 "state_map_version": bundle.state_map.version, "state_map_sha256": bundle.state_map.sha256,
                 "fixture": bundle.is_fixture},
        "start_state_sha256": bundle.start_state_sha256,
        "game_seed": None,
        "provider": c.provider,
        "model_id": c.model_id,
        "preset": c.preset_ref,
        "preset_text_sha256": preset.text_sha256,
        "preset_label": preset.label,
        "base_prompt": {"version": BASE_PROMPT_VERSION, "sha256": BASE_PROMPT_SHA256},
        "memory": {"enabled": c.memory.enabled, "max_files": c.memory.max_files,
                   "max_file_chars": c.memory.max_file_chars, "max_total_chars": c.memory.max_total_chars,
                   "max_history_rows": c.memory.max_history_rows, "max_history_chars": c.memory.max_history_chars,
                   "inject_path": c.inject_path, "inject_cap_chars": c.inject_cap_chars},
        "sessions": sessions or {"session_len": None, "dream": False},
        "observation": {"screenshot_policy": c.screenshots.mode, "k": c.screenshots.k,
                        "frame_scale": c.screenshots.frame_scale, "hide_extras": list(c.observation.hide_extras),
                        "show_milestones": c.observation.show_milestones},
        "caps": {"max_steps": c.caps.max_steps, "max_tokens": c.caps.max_tokens, "max_wall_s": c.caps.max_wall_s,
                 "max_output_tokens": c.caps.max_output_tokens,
                 "max_tool_calls_per_step": c.caps.max_tool_calls_per_step, "context_steps": c.caps.context_steps,
                 "call_timeout_s": c.caps.call_timeout_s, "max_total_frames": max_total_frames},
        "parent": parent,
        "config_hash": f"sha256:{c.config_hash}",
    }


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return canonical_json(manifest)


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def _key(run_id: str) -> str:
    return f"runs/{run_id}/manifest.json"


def seal_manifest(store: ObjectStore, run_id: str, manifest: dict[str, Any]) -> str:
    """Write once. A second seal (even with identical content) is refused."""
    if manifest.get("run_id") != run_id:
        raise ValueError("manifest belongs to a different run")
    try:
        store.put(_key(run_id), manifest_bytes(manifest))
    except AlreadyExists:
        raise ManifestSealed(run_id) from None
    return manifest_sha256(manifest)


def load_manifest(store: ObjectStore, run_id: str) -> dict[str, Any]:
    doc = json.loads(store.get(_key(run_id)))  # NotFound propagates
    if not isinstance(doc, dict) or doc.get("run_id") != run_id:
        raise ValueError("manifest does not belong to this run")
    return doc
