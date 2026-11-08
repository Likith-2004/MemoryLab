import threading

import pytest
from labharness.providers.secret import SecretKey

from conftest import CANARY, SECRET, make_config
from orchestrator.config import NAME_PREFIX, OWNER_LABEL, OWNER_VALUE, SandboxResources
from orchestrator.dockercmd import build_run_command, container_name
from orchestrator.keybox import KeyBox
from orchestrator.scheduler import (
    BreakerOpen,
    CapacityFull,
    Duplicate,
    IpBusy,
    RunRequest,
    Scheduler,
    UserBusy,
)

RUN = "11111111-2222-3333-4444-555555555555"


# ---- docker command ------------------------------------------------------------------------------
def test_the_run_command_has_every_hardening_flag() -> None:
    cmd = build_run_command(make_config(), RUN)
    assert cmd[:4] == ["docker", "run", "-i", "--rm"] and cmd[-1] == "memorylab-test-image"
    pairs = {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1)}
    assert pairs["--network"] == "memorylab-test-net" and pairs["--user"] == "10001:10001"
    assert pairs["--cap-drop"] == "ALL" and pairs["--security-opt"] == "no-new-privileges"
    assert (pairs["--pids-limit"], pairs["--memory"], pairs["--memory-swap"], pairs["--cpus"]) == ("128", "1g", "1g", "1")
    assert "--read-only" in cmd and "--init" in cmd and pairs["--log-driver"] == "none"
    assert pairs["--tmpfs"].startswith("/tmp:rw,noexec,nosuid,nodev,size=64m")
    assert pairs["--name"] == f"{NAME_PREFIX}run-{RUN}"
    assert f"{OWNER_LABEL}={OWNER_VALUE}" in cmd and f"memorylab.run={RUN}" in cmd
    assert "--privileged" not in cmd and "--network=host" not in cmd and "-v" not in cmd and "--volume" not in cmd
    assert "--runtime" not in cmd and "--env" in cmd                                      # fake provider flag (tests only)


def test_no_secret_can_be_part_of_the_command() -> None:
    cmd = build_run_command(make_config(allow_fake_provider=False), RUN)
    assert "--env" not in cmd and "-e" not in cmd and CANARY not in " ".join(cmd)
    assert all(SECRET.decode() not in part for part in cmd)


def test_runtime_flag_is_added_when_configured() -> None:
    cmd = build_run_command(make_config(resources=SandboxResources(runtime="runsc")), RUN)
    assert cmd[cmd.index("--runtime") + 1] == "runsc"


@pytest.mark.parametrize("bad", ["", "../x", "nope", RUN + "x", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE", "a;b"])
def test_run_ids_are_validated_before_they_reach_a_command(bad: str) -> None:
    with pytest.raises(ValueError):
        container_name(bad)
    with pytest.raises(ValueError):
        build_run_command(make_config(), bad)


# ---- key box ---------------------------------------------------------------------------------------
def test_key_box_hands_the_key_over_exactly_once() -> None:
    box = KeyBox(SecretKey(CANARY))
    assert not box.empty and CANARY not in repr(box)
    assert box.take().reveal() == CANARY and box.empty
    with pytest.raises(RuntimeError):
        box.take()


def test_key_box_discard_destroys_the_reference() -> None:
    box = KeyBox(SecretKey(CANARY))
    box.discard()
    assert box.empty
    with pytest.raises(RuntimeError):
        box.take()
    box.discard()


# ---- scheduler ----------------------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def req(n: int, user: str | None = None, ip: str | None = None, at: float = 0.0) -> RunRequest:
    return RunRequest(f"00000000-0000-0000-0000-{n:012d}", user or f"u{n}", ip or f"ip{n}", "fixture", None, None, "fake",
                      KeyBox(SecretKey(CANARY)), None, at)


def test_fifo_and_global_concurrency() -> None:
    s = Scheduler(make_config(max_concurrent=2, max_queue=10), Clock())
    for i in range(4):
        assert s.submit(req(i)) == i
    first, second = s.next_ready(), s.next_ready()
    assert [first.run_id[-1], second.run_id[-1]] == ["0", "1"] and s.next_ready() is None   # cap of 2 active
    s.finished(first.run_id)
    assert s.next_ready().run_id.endswith("2")


def test_per_user_and_per_ip_limits_count_queued_and_active_runs() -> None:
    s = Scheduler(make_config(per_user=1, per_ip=2), Clock())
    s.submit(req(1, user="a", ip="x"))
    with pytest.raises(UserBusy):
        s.submit(req(2, user="a", ip="y"))
    s.submit(req(3, user="b", ip="x"))
    with pytest.raises(IpBusy):
        s.submit(req(4, user="c", ip="x"))
    started = s.next_ready()                                      # moving to active still counts
    with pytest.raises(UserBusy):
        s.submit(req(5, user=started.user_id, ip="z"))
    s.finished(started.run_id)
    s.submit(req(6, user=started.user_id, ip="z"))                # freed once finished


def test_queue_limit_returns_capacity_full_with_retry_after() -> None:
    s = Scheduler(make_config(max_queue=2), Clock())
    s.submit(req(1)); s.submit(req(2))
    with pytest.raises(CapacityFull) as info:
        s.submit(req(3))
    assert info.value.retry_after_s == 60 and info.value.status == 429 and info.value.code == "capacity_full"


def test_duplicate_run_ids_are_refused() -> None:
    s = Scheduler(make_config(), Clock())
    s.submit(req(1))
    with pytest.raises(Duplicate):
        s.submit(req(1, user="other", ip="other"))


def test_queue_ttl_destroys_keys_and_frees_the_slot() -> None:
    clock = Clock()
    s = Scheduler(make_config(queue_ttl_s=600), clock)
    old, fresh = req(1, at=0.0), req(2, at=500.0)
    s.submit(old); s.submit(fresh)
    clock.t = 599.9
    assert s.expire() == [] and not old.key_box.empty
    clock.t = 600.0
    assert [r.run_id for r in s.expire()] == [old.run_id] and old.key_box.empty and not fresh.key_box.empty
    assert s.snapshot()["queued"] == [fresh.run_id]


def test_breaker_blocks_admission_and_starts_but_not_running_work() -> None:
    s = Scheduler(make_config(), Clock())
    s.submit(req(1))
    with pytest.raises(ValueError, match="reason"):
        s.set_breaker(open_=True, reason="  ", actor="admin")
    b = s.set_breaker(open_=True, reason="provider outage", actor="admin")
    assert b.open and s.next_ready() is None                       # nothing new starts
    with pytest.raises(BreakerOpen):
        s.submit(req(2))
    s.set_breaker(open_=False, reason="", actor="admin")
    assert s.next_ready() is not None and not s.breaker.open


def test_cancel_queued_discards_the_key() -> None:
    s = Scheduler(make_config(), Clock())
    r = req(1)
    s.submit(r)
    assert s.cancel_queued(r.run_id) is r and r.key_box.empty and s.cancel_queued(r.run_id) is None


def test_concurrent_submissions_never_exceed_the_per_user_limit() -> None:
    s = Scheduler(make_config(per_user=1, per_ip=100, max_queue=100), Clock())
    accepted = []

    def go(n: int) -> None:
        try:
            s.submit(req(n, user="same", ip=f"ip{n}"))
            accepted.append(n)
        except UserBusy:
            pass

    threads = [threading.Thread(target=go, args=(n,)) for n in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(accepted) == 1
