"""Orchestrator configuration. Service secrets (token secret, API token) are provided by the
deployment; visitor keys never appear here."""

from dataclasses import dataclass, field

# Label put on every Docker object we create. Cleanup only ever touches objects carrying it, so it
# can never affect other projects sharing the Docker daemon.
OWNER_LABEL = "memorylab.owner"
OWNER_VALUE = "memorylab"
NAME_PREFIX = "memorylab-"


@dataclass(frozen=True)
class ServerLimits:
    """Upper bounds the platform enforces on every run, whatever the request asks for."""

    max_steps: int = 2000
    max_tokens: int = 4_000_000
    max_wall_s: float = 7200.0
    max_output_tokens: int = 4096
    max_tool_calls_per_step: int = 8
    max_context_steps: int = 20


@dataclass(frozen=True)
class SandboxResources:
    memory: str = "1g"
    cpus: str = "1"
    pids: int = 128
    tmpfs_mb: int = 64
    user: str = "10001:10001"
    runtime: str | None = None   # "runsc" when gVisor is installed (it is not on this machine: DEVIATION M5-1)
    stop_timeout_s: int = 5


@dataclass(frozen=True)
class OrchestratorConfig:
    image: str
    network: str                       # a Docker network created with --internal
    proxy_url: str                     # http://<egress proxy container>:3128, reachable on that network
    sandbox_ingest_url: str            # ingest as the sandbox reaches it (through the proxy)
    ingest_url: str                    # ingest as the orchestrator reaches it
    games_dir: str
    token_secret: bytes
    max_concurrent: int = 8
    max_queue: int = 32
    queue_ttl_s: float = 600.0
    per_user: int = 1
    per_ip: int = 2
    heartbeat_timeout_s: float = 30.0
    startup_grace_s: float = 60.0
    wall_grace_s: float = 30.0
    heartbeat_period_s: float = 10.0
    limits: ServerLimits = field(default_factory=ServerLimits)
    resources: SandboxResources = field(default_factory=SandboxResources)
    allow_fake_provider: bool = False  # tests only
    docker: str = "docker"
