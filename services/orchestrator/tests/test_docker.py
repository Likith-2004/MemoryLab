"""Integration tests against a real local Docker daemon.

They create ONLY objects named/labelled `memorylab` (images memorylab-*:dev, an internal network,
containers) and remove only those; the daemon may host other projects, which are never touched.
Build the images first: infra/docker/build.sh. Skipped when Docker or the images are missing.
"""

import json
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn
from ingest.app import create_app as create_ingest_app
from ingest.repository import InMemoryRunRepository
from ingest.service import IngestService
from labharness import load_bundle
from runlog.bundle import verify_bundle
from runlog.eventlog import EventLogReader
from runlog.replay import reexecute, verify_log
from runlog.store import LocalObjectStore

from conftest import CANARY, GAMES, RUN_CONFIG, SECRET, submission
from orchestrator.config import OWNER_LABEL, OWNER_VALUE, OrchestratorConfig, SandboxResources
from orchestrator.core import Orchestrator
from orchestrator.dockercmd import build_run_command, container_name
from orchestrator.process import PopenProcess

pytestmark = pytest.mark.docker
WORKER_IMAGE, PROXY_IMAGE = "memorylab-worker:dev", "memorylab-egress:dev"


def docker(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def have_images() -> bool:
    try:
        return all(docker("image", "inspect", i, check=False).returncode == 0 for i in (WORKER_IMAGE, PROXY_IMAGE))
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not have_images(), reason="build images first: infra/docker/build.sh")]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


class Stack:
    """Internal network + egress proxy container + a real ingest server on the host."""

    def __init__(self, tmp: Path) -> None:
        self.tag = uuid.uuid4().hex[:8]
        self.net = f"memorylab-it-{self.tag}"
        self.proxy_name = f"memorylab-it-egress-{self.tag}"
        self.repo = InMemoryRunRepository()
        self.store = LocalObjectStore(tmp / "objects")
        self.port = free_port()
        service = IngestService(self.repo, self.store, SECRET)
        self.server = uvicorn.Server(uvicorn.Config(create_ingest_app(service), host="0.0.0.0", port=self.port,
                                                    log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def up(self) -> None:
        self.thread.start()
        deadline = time.time() + 30
        while True:
            # the server thread may not be listening yet: a refused connection means "not yet", not a failure (this raced and failed a gate run)
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=2).is_success:
                    break
            except httpx.TransportError:
                pass
            assert time.time() < deadline, "the test ingest server did not come up"
            time.sleep(0.1)
        label = f"{OWNER_LABEL}={OWNER_VALUE}"
        docker("network", "create", "--internal", "--label", label, self.net)
        docker("run", "-d", "--name", self.proxy_name, "--label", label, "--network", self.net,
               "--add-host", "host.docker.internal:host-gateway", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--memory", "128m", PROXY_IMAGE,
               "--listen", "0.0.0.0:3128", "--allow-private", f"host.docker.internal:{self.port}")
        docker("network", "connect", "bridge", self.proxy_name)               # the proxy alone has a route out
        time.sleep(1.0)

    def down(self) -> None:
        self.server.should_exit = True
        docker("rm", "-f", self.proxy_name, check=False)
        for cid in docker("ps", "-aq", "--filter", f"network={self.net}", check=False).stdout.split():
            docker("rm", "-f", cid, check=False)
        docker("network", "rm", self.net, check=False)

    def config(self, **over: object) -> OrchestratorConfig:
        base: dict[str, object] = {
            "image": WORKER_IMAGE, "network": self.net, "proxy_url": f"http://{self.proxy_name}:3128",
            "sandbox_ingest_url": f"http://host.docker.internal:{self.port}", "ingest_url": f"http://127.0.0.1:{self.port}",
            "games_dir": str(GAMES), "token_secret": SECRET, "allow_fake_provider": True, "heartbeat_period_s": 0.5,
            "resources": SandboxResources(memory="512m"), "startup_grace_s": 90.0}
        base.update(over)
        return OrchestratorConfig(**base)  # type: ignore[arg-type]

    def orch(self, **over: object) -> Orchestrator:
        return Orchestrator(self.config(**over), httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=30))

    def register(self) -> str:
        run_id = str(uuid.uuid4())
        self.repo.register(run_id)
        return run_id


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    s = Stack(tmp_path_factory.mktemp("dockerit"))
    try:
        s.up()
        yield s
    finally:
        s.down()


def states(stack: Stack, run_id: str) -> list[tuple[str, str | None]]:
    return [(e.data["state"], e.data.get("reason")) for e in EventLogReader(stack.store, run_id).iter_events() if e.type == "run.state"]


def probe(stack: Stack, code: str, *, memory: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run python code inside a sandbox with exactly the production flags (entrypoint swapped)."""
    cfg = stack.config(resources=SandboxResources(memory=memory or "512m"))
    run_id = str(uuid.uuid4())
    argv = build_run_command(cfg, run_id)
    argv = argv[:-1] + ["--entrypoint", "python", cfg.image, "-c", code]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    finally:
        docker("rm", "-f", container_name(run_id), check=False)


# ---- a whole run in a real container -----------------------------------------------------------------------
def test_a_full_run_in_a_real_sandbox(stack: Stack) -> None:
    run_id = stack.register()
    orch = stack.orch()
    orch.submit(submission(run_id))
    orch.tick()
    assert orch.wait_idle(240)
    assert states(stack, run_id) == [("queued", None), ("starting", None), ("running", None), ("stopping", "steps"), ("capped", "steps")]
    rec = stack.repo.get(run_id)
    assert rec.closed and rec.steps == 4 and rec.frames >= 1
    events = list(EventLogReader(stack.store, run_id).iter_events())
    assert verify_log(events) == [] and verify_bundle(stack.store.get(f"runs/{run_id}/bundle.zip")).ok
    assert reexecute(events, load_bundle(GAMES / "fixture"))["ok"]                      # the container's run replays exactly
    leftovers = docker("ps", "-aq", "--filter", f"label=memorylab.run={run_id}").stdout.split()
    assert leftovers == []                                                              # destroyed with the run
    blob = b"".join(p.read_bytes() for p in stack.store.root.rglob("*") if p.is_file())
    assert CANARY.encode() not in blob


def test_the_running_sandbox_is_hardened_and_holds_no_key(stack: Stack) -> None:
    run_id = stack.register()
    orch = stack.orch()
    script = [{"calls": [["wait", {"frames": 600}]], "tokens": 10}] * 400
    orch.submit(submission(run_id, run_config={**RUN_CONFIG, "caps": {"max_steps": 400}},
                           provider={"name": "fake", "api_key": CANARY, "script": script}))
    orch.tick()
    name = container_name(run_id)
    info = None
    deadline = time.time() + 120
    while time.time() < deadline:
        out = docker("inspect", name, check=False)
        if out.returncode == 0 and json.loads(out.stdout)[0]["State"]["Running"] and stack.repo.get(run_id).steps >= 1:
            info = json.loads(out.stdout)[0]
            break
        time.sleep(0.3)
    try:
        assert info is not None, "container never reached the running state"
        host, cfg = info["HostConfig"], info["Config"]
        assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
        assert any(opt.startswith("no-new-privileges") for opt in host["SecurityOpt"])
        assert host["PidsLimit"] == 128 and host["Memory"] == 512 * 1024**2 and host["MemorySwap"] == 512 * 1024**2
        assert host["NanoCpus"] == 1_000_000_000 and host["Privileged"] is False and host["Init"] is True
        assert host["NetworkMode"] == stack.net and not host.get("Binds") and not host.get("PortBindings")
        assert host["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=64m"} and host["LogConfig"]["Type"] == "none"
        assert cfg["User"] == "10001:10001" and info["Mounts"] == [] and host.get("Devices") in (None, [])
        assert cfg["Labels"][OWNER_LABEL] == OWNER_VALUE and info["Name"] == "/" + name
        blob = json.dumps(info)
        assert CANARY not in blob                                                       # not in env, cmd, labels, anything
        assert all("=" in e and not e.upper().startswith(("ANTHROPIC", "API_KEY", "TOKEN")) for e in cfg["Env"])
        top = docker("top", name, "-eo", "args", check=False).stdout
        assert CANARY not in top and "ingest" not in top                                # not in any process arguments
        procs = docker("exec", name, "cat", "/proc/1/environ", check=False)
        assert CANARY not in procs.stdout
        assert orch.kill(run_id, "test", "end of test") == "killed"
    finally:
        orch.kill(run_id, "test", "cleanup")
        orch.wait_idle(60)
    assert docker("ps", "-aq", "--filter", f"name={name}").stdout.split() == []


# ---- probes from inside the sandbox ---------------------------------------------------------------------------------
PROBE = r"""
import json, os, socket, subprocess, sys
out = {}
out["uid"] = os.getuid()
status = dict(l.split(":\t", 1) for l in open("/proc/self/status").read().splitlines() if ":\t" in l)
out["cap_eff"] = status["CapEff"].strip()
out["no_new_privs"] = status.get("NoNewPrivs", "").strip()
for path in ("/opt/probe", "/usr/local/probe", "/etc/probe"):
    try:
        open(path, "w").write("x"); out[path] = "WRITABLE"
    except OSError as e:
        out[path] = type(e).__name__
open("/tmp/ok", "w").write("x"); out["tmp_writable"] = True
mounts = [l.split() for l in open("/proc/mounts")]
out["tmp_opts"] = next(m[3] for m in mounts if m[1] == "/tmp")
out["root_opts"] = next(m[3] for m in mounts if m[1] == "/")
out["docker_sock"] = os.path.exists("/var/run/docker.sock")
out["home_listing"] = sorted(os.listdir("/home")) if os.path.isdir("/home") else None
out["env_keys"] = sorted(k for k in os.environ if any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASS")))
def tcp(host, port, timeout=3):
    try:
        socket.create_connection((host, port), timeout).close(); return "connected"
    except OSError as e:
        return type(e).__name__
out["direct_1.1.1.1"] = tcp("1.1.1.1", 443)
out["direct_8.8.8.8_dns"] = tcp("8.8.8.8", 53)
out["direct_gateway"] = tcp(os.popen("ip route 2>/dev/null | awk '/default/ {print $3}'").read().strip() or "172.17.0.1", 80)
try:
    socket.getaddrinfo("example.com", 80); out["dns_example"] = "resolved"
except OSError as e:
    out["dns_example"] = type(e).__name__
proxy = os.environ.get("PROBE_PROXY"); host, port = proxy.split(":")
out["proxy_tcp"] = tcp(host, int(port))
def via_proxy(request: bytes) -> str:
    s = socket.create_connection((host, int(port)), 5); s.sendall(request); s.settimeout(5)
    data = s.recv(4096).split(b"\r\n", 1)[0].decode(); s.close(); return data
out["connect_example"] = via_proxy(b"CONNECT example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
out["connect_anthropic"] = via_proxy(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
out["connect_ip"] = via_proxy(b"CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: x\r\n\r\n")
ing = os.environ["PROBE_INGEST"]
out["http_ingest_via_proxy"] = via_proxy(f"GET http://{ing}/healthz HTTP/1.1\r\nHost: {ing}\r\n\r\n".encode())
out["http_example_via_proxy"] = via_proxy(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
print(json.dumps(out))
"""


def test_probes_from_inside_the_sandbox(stack: Stack) -> None:
    code = PROBE
    cfg = stack.config()
    argv_env = ["--env", f"PROBE_PROXY={stack.proxy_name}:3128", "--env", f"PROBE_INGEST=host.docker.internal:{stack.port}"]
    run_id = str(uuid.uuid4())
    argv = build_run_command(cfg, run_id)
    argv = argv[:-1] + argv_env + ["--entrypoint", "python", cfg.image, "-c", code]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    finally:
        docker("rm", "-f", container_name(run_id), check=False)
    assert proc.returncode == 0, proc.stderr[-500:]
    r = json.loads(proc.stdout.strip().splitlines()[-1])
    assert r["uid"] == 10001 and r["cap_eff"] == "0000000000000000" and r["no_new_privs"] == "1"
    assert all(r[p] != "WRITABLE" for p in ("/opt/probe", "/usr/local/probe", "/etc/probe")) and r["tmp_writable"]
    assert "ro" in r["root_opts"].split(",") and {"noexec", "nosuid", "nodev"} <= set(r["tmp_opts"].split(","))
    assert r["docker_sock"] is False and r["home_listing"] in (None, [])
    assert r["env_keys"] == []                                                          # no secrets in the environment
    for direct in ("direct_1.1.1.1", "direct_8.8.8.8_dns", "direct_gateway"):
        assert r[direct] != "connected", direct                                         # no route out except via the proxy
    assert r["dns_example"] != "resolved"                                               # no DNS beyond the proxy
    assert r["proxy_tcp"] == "connected"
    assert r["connect_example"].startswith("HTTP/1.1 403") and r["connect_anthropic"].startswith("HTTP/1.1 403")
    assert r["connect_ip"].startswith("HTTP/1.1 403") and r["http_example_via_proxy"].startswith("HTTP/1.1 403")
    assert r["http_ingest_via_proxy"].startswith("HTTP/1.1 200")                        # the one allowed target works


def test_the_pids_limit_stops_a_fork_bomb(stack: Stack) -> None:
    code = ("import subprocess, json\nprocs=[]\nfor i in range(400):\n"
            "    try: procs.append(subprocess.Popen(['sleep','20']))\n    except OSError: break\n"
            "print(json.dumps({'spawned': len(procs)}))\n[p.kill() for p in procs]\n")
    proc = probe(stack, code)
    spawned = json.loads(proc.stdout.strip().splitlines()[-1])["spawned"]
    assert 50 < spawned < 128, spawned                                                  # capped by --pids-limit 128


def test_the_memory_limit_kills_a_runaway_allocation(stack: Stack) -> None:
    proc = probe(stack, "x = bytearray(900 * 1024 * 1024)\nprint('allocated')", memory="128m")
    assert proc.returncode == 137 and "allocated" not in proc.stdout                    # OOM-killed by the cgroup


# ---- supervision against a real container -------------------------------------------------------------------------------------
def test_missed_heartbeats_kill_a_real_container(stack: Stack) -> None:
    run_id = stack.register()
    code = "import time\nprint('{\"hb\":1}', flush=True)\ntime.sleep(600)\n"

    def launch(argv: list[str]) -> PopenProcess:
        return PopenProcess(argv[:-1] + ["--entrypoint", "python", WORKER_IMAGE, "-c", code])

    cfg = stack.config(heartbeat_timeout_s=3.0)
    orch = Orchestrator(cfg, httpx.Client(base_url=f"http://127.0.0.1:{stack.port}", timeout=30), launcher=launch)
    orch.submit(submission(run_id))
    orch.tick()
    assert orch.wait_idle(120)
    assert states(stack, run_id)[-1] == ("failed", "heartbeat_timeout")
    assert docker("ps", "-aq", "--filter", f"label=memorylab.run={run_id}").stdout.split() == []   # killed and removed
    assert stack.repo.get(run_id).closed


def test_the_orchestrator_refuses_a_network_with_a_route_out(stack: Stack) -> None:
    run_id = stack.register()
    orch = stack.orch(network="bridge")                                                 # the default bridge is not internal
    orch.submit(submission(run_id))
    orch.tick()
    assert orch.wait_idle(60)
    assert states(stack, run_id)[-1] == ("failed", "DockerError")
    assert docker("ps", "-aq", "--filter", f"label=memorylab.run={run_id}").stdout.split() == []


def test_docker_helpers_touch_only_our_own_named_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import dockercmd
    calls: list[list[str]] = []
    # `container inspect` "fails" (returncode 1) so the helper sees the container as gone
    monkeypatch.setattr(dockercmd.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 1 if "inspect" in argv else 0, "", ""))
    run_id = str(uuid.uuid4())
    cfg = OrchestratorConfig(WORKER_IMAGE, "n", "http://p:1", "http://i:1", "http://i:1", str(GAMES), SECRET)
    dockercmd.remove_container(cfg, run_id)
    dockercmd.kill_container(cfg, run_id)
    assert calls == [["docker", "rm", "-f", f"memorylab-run-{run_id}"], ["docker", "container", "inspect", f"memorylab-run-{run_id}"],
                     ["docker", "kill", f"memorylab-run-{run_id}"]]
    with pytest.raises(ValueError):
        dockercmd.remove_container(cfg, "sbam-run-worker-1")                           # another project's name is refused outright


def test_a_fork_resumes_from_its_parents_checkpoint_in_real_containers(stack: Stack) -> None:
    """B-6 end to end: the real orchestrator, two real sandboxes (parent, then child), the real ingest service and the real emulator."""
    from ingest.repository import ParentRef
    cfg = {**RUN_CONFIG, "caps": {"max_steps": 4}, "sessions": {"session_len": 2}}
    steps = [{"calls": [["press", {"buttons": ["Right"], "hold_frames": 1}]] + ([["mem_write", {"path": "notes/a.md", "content": "door\n"}]] if i == 0 else []),
              "text": f"s{i + 1}", "tokens": 100} for i in range(4)]
    parent = stack.register()
    orch = stack.orch()
    orch.submit(submission(parent, run_config=cfg, provider={"name": "fake", "api_key": CANARY, "script": steps}))
    orch.tick()
    assert orch.wait_idle(240)
    assert states(stack, parent)[-1] == ("capped", "steps")
    parent_events = list(EventLogReader(stack.store, parent).iter_events())
    cp = next(e for e in parent_events if e.type == "checkpoint" and e.step == 2)

    child = str(uuid.uuid4())
    stack.repo.register(child, parent=ParentRef(parent, 2, cp.seq, "cp-1"))
    sub = submission(child, run_config=cfg, provider={"name": "fake", "api_key": CANARY, "script": steps[2:]})
    sub = type(sub)(**{**sub.__dict__, "parent": {"run_id": parent, "checkpoint_id": "cp-1", "step": 2}})
    orch2 = stack.orch()
    orch2.submit(sub)
    orch2.tick()
    assert orch2.wait_idle(240)
    assert states(stack, child)[-1] == ("capped", "steps"), states(stack, child)
    child_events = list(EventLogReader(stack.store, child).iter_events())
    assert verify_log(child_events) == [] and verify_bundle(stack.store.get(f"runs/{child}/bundle.zip")).ok
    last = lambda evs: [e for e in evs if e.type == "obs"][-1].data
    assert last(child_events)["ram_sha256"] == last(parent_events)["ram_sha256"]            # same emulator state after the same steps
    assert next(e for e in child_events if e.type == "fork.start").data["parent_run_id"] == parent
    assert docker("ps", "-aq", "--filter", f"label=memorylab.run={child}").stdout.split() == []
    blob = b"".join(p.read_bytes() for p in stack.store.root.rglob("*") if p.is_file())
    assert CANARY.encode() not in blob
