"""Docker command construction and network checks. Pure functions where possible so the exact
hardening flags can be asserted without a daemon."""

import json
import re
import subprocess
import time
from dataclasses import dataclass

from .config import NAME_PREFIX, OWNER_LABEL, OWNER_VALUE, OrchestratorConfig

_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class DockerError(Exception):
    pass


def container_name(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("bad run id")
    return f"{NAME_PREFIX}run-{run_id}"


def build_run_command(cfg: OrchestratorConfig, run_id: str) -> list[str]:
    """`docker run` for one sandbox. Nothing secret is ever part of this command: the job spec
    (with the visitor key) goes to the container's stdin."""
    r = cfg.resources
    cmd = [
        cfg.docker, "run", "-i", "--rm",
        "--name", container_name(run_id),
        "--label", f"{OWNER_LABEL}={OWNER_VALUE}", "--label", f"memorylab.run={run_id}",
        "--network", cfg.network,
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(r.pids),
        "--memory", r.memory, "--memory-swap", r.memory,
        "--cpus", r.cpus,
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={r.tmpfs_mb}m",
        "--user", r.user,
        "--ulimit", "nofile=1024:1024", "--ulimit", "core=0",
        "--log-driver", "none",
        "--stop-timeout", str(r.stop_timeout_s),
        "--init",
    ]
    if r.runtime:
        cmd += ["--runtime", r.runtime]
    if cfg.allow_fake_provider:
        cmd += ["--env", "MEMORYLAB_ALLOW_FAKE_PROVIDER=1"]  # not a secret; tests only
    cmd.append(cfg.image)
    return cmd


def remove_container(cfg: OrchestratorConfig, run_id: str, wait_s: float = 15.0) -> bool:
    """Force-remove one of OUR containers by exact name and confirm it is gone (this can race with
    `--rm` auto-removal, which reports "removal already in progress"). Returns True once absent."""
    name = container_name(run_id)
    deadline = time.monotonic() + wait_s
    while True:
        subprocess.run([cfg.docker, "rm", "-f", name], capture_output=True, timeout=30, check=False)
        gone = subprocess.run([cfg.docker, "container", "inspect", name], capture_output=True, timeout=30,
                              check=False).returncode != 0
        if gone or time.monotonic() >= deadline:
            return gone
        time.sleep(0.25)


def kill_container(cfg: OrchestratorConfig, run_id: str) -> None:
    subprocess.run([cfg.docker, "kill", container_name(run_id)], capture_output=True, timeout=30, check=False)


@dataclass(frozen=True)
class NetworkInfo:
    name: str
    internal: bool
    owned: bool


def inspect_network(cfg: OrchestratorConfig) -> NetworkInfo:
    proc = subprocess.run([cfg.docker, "network", "inspect", cfg.network], capture_output=True, text=True,
                          timeout=30, check=False)
    if proc.returncode != 0:
        raise DockerError("sandbox network does not exist")
    doc = json.loads(proc.stdout)[0]
    return NetworkInfo(doc["Name"], bool(doc.get("Internal")), (doc.get("Labels") or {}).get(OWNER_LABEL) == OWNER_VALUE)


def require_internal_network(cfg: OrchestratorConfig) -> None:
    """Fail closed: refuse to start sandboxes on a network that has a route out."""
    info = inspect_network(cfg)
    if not info.internal:
        raise DockerError(f"network {info.name!r} is not internal: refusing to run sandboxes on it")


def ensure_internal_network(cfg: OrchestratorConfig) -> None:
    """Create our internal network if it does not exist (never modifies an existing one)."""
    probe = subprocess.run([cfg.docker, "network", "inspect", cfg.network], capture_output=True, timeout=30, check=False)
    if probe.returncode != 0:
        created = subprocess.run([cfg.docker, "network", "create", "--internal", "--label", f"{OWNER_LABEL}={OWNER_VALUE}",
                                  cfg.network], capture_output=True, timeout=30, check=False)
        if created.returncode != 0:
            raise DockerError("could not create the sandbox network")
    require_internal_network(cfg)
