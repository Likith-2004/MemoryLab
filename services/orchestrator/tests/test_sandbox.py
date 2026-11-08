import json
import time

from conftest import FakeProcess, make_config
from orchestrator.sandbox import Sandbox


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def sandbox(proc: FakeProcess, clock: Clock | None = None, wall: float = 100.0, **cfg: float) -> tuple[Sandbox, list[int]]:
    kills: list[int] = []
    sb = Sandbox(proc, make_config(**cfg), wall_limit_s=wall, kill_container=lambda: kills.append(1),
                 clock=clock or time.monotonic, poll_s=0.01)
    return sb, kills


def hb(n: int) -> bytes:
    return json.dumps({"hb": n}).encode() + b"\n"


def test_a_clean_run_reports_done_and_heartbeats() -> None:
    proc = FakeProcess([hb(1), hb(2), b'{"done":{"state":"capped","reason":"steps","steps":4,"tokens":400,"redaction_hits":0,"extra":"x"}}\n'], 0, run_for=0.1)
    sb, kills = sandbox(proc)
    r = sb.run()
    assert (r.exit_code, r.heartbeats, r.killed_reason, kills) == (0, 2, None, [])
    assert r.done == {"state": "capped", "reason": "steps", "steps": 4, "tokens": 400, "redaction_hits": 0}   # unknown fields dropped


def test_the_job_is_written_first_and_stop_is_a_control_line() -> None:
    proc = FakeProcess([], 0, run_for=0.1)
    sb, _ = sandbox(proc)
    sb.send_job(b'{"v":1}')
    sb.stop()
    assert bytes(proc.stdin) == b'{"v":1}\n{"cmd":"stop"}\n'
    sb.run()


def test_silence_after_the_first_heartbeat_kills_the_container() -> None:
    clock = Clock()
    proc = FakeProcess([hb(1)], hang=True)
    sb, kills = sandbox(proc, clock, heartbeat_timeout_s=30.0)
    import threading
    t = threading.Thread(target=sb.run)
    t.start()
    time.sleep(0.1)
    clock.t = 29.0
    time.sleep(0.1)
    assert not proc.killed                                           # 29 s of silence is still fine
    clock.t = 31.0
    t.join(5)
    assert proc.killed and kills and sb.result.killed_reason == "heartbeat_timeout"


def test_a_worker_that_never_starts_beating_is_killed_after_the_startup_grace() -> None:
    clock = Clock()
    proc = FakeProcess([], hang=True)
    sb, kills = sandbox(proc, clock, startup_grace_s=60.0)
    import threading
    t = threading.Thread(target=sb.run)
    t.start()
    time.sleep(0.1)
    clock.t = 59.0
    time.sleep(0.1)
    assert not proc.killed
    clock.t = 61.0
    t.join(5)
    assert sb.result.killed_reason == "startup_timeout" and kills


def test_the_wall_clock_deadline_kills_even_a_healthy_worker() -> None:
    clock = Clock()
    proc = FakeProcess([hb(1)], hang=True)
    sb, _ = sandbox(proc, clock, wall=100.0, wall_grace_s=30.0)
    import threading
    t = threading.Thread(target=sb.run)
    t.start()
    time.sleep(0.1)
    clock.t = 129.0
    sb._last_beat = clock.t          # still beating: only the wall clock applies
    time.sleep(0.1)
    assert not proc.killed
    clock.t = 131.0
    sb._last_beat = clock.t
    t.join(5)
    assert sb.result.killed_reason == "wall_clock"


def test_admin_kill_records_its_reason_and_kills_the_container() -> None:
    proc = FakeProcess([hb(1)], hang=True)
    sb, kills = sandbox(proc)
    import threading
    t = threading.Thread(target=sb.run)
    t.start()
    time.sleep(0.1)
    sb.kill("admin_kill")
    t.join(5)
    assert sb.result.killed_reason == "admin_kill" and kills and sb.result.exit_code == -9


def test_garbage_and_oversized_output_is_dropped_never_trusted() -> None:
    lines = [b"hello\n", b"[]\n", b'{"hb":"x"}\n', b'{"hb":1,"x":2}\n', b'{"cmd":"explode"}\n', b'{"done":5}\n', b"\xff\xfe\n",
             b'{"fatal":"' + b"A" * 200 + b'"}\n', hb(1)]
    proc = FakeProcess(lines, 0, run_for=0.1)
    r = sandbox(proc)[0].run()
    assert r.garbage_lines == 7 and r.heartbeats == 1 and r.done is None and len(r.fatal or "") == 60


def test_fatal_is_a_type_name_only() -> None:
    r = sandbox(FakeProcess([b'{"fatal":"RuntimeError"}\n'], 1, run_for=0.1))[0].run()
    assert (r.fatal, r.exit_code) == ("RuntimeError", 1)


def test_a_sandbox_that_stops_heartbeating_is_killed_within_the_timeout_on_the_real_clock() -> None:
    import threading
    timeout = 0.3
    proc = FakeProcess([hb(1)], hang=True)
    sb, kills = sandbox(proc, wall=100.0, heartbeat_timeout_s=timeout, startup_grace_s=100.0)
    t = threading.Thread(target=sb.run, daemon=True)               # a missing check must fail the test, not hang it
    started = time.monotonic()
    t.start()
    t.join(timeout + 0.5)
    elapsed = time.monotonic() - started
    proc.killed = True                                              # release the thread if the check was absent
    assert not t.is_alive() and elapsed >= timeout, "sandbox was not killed within heartbeat_timeout_s"
    assert sb.result.killed_reason == "heartbeat_timeout" and kills
