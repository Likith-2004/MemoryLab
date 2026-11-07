"""Milestone 1 close-out: cartridge RAM in the digest, tick() ended, behavioural idle frames,
CLI, and ROM RAM-layout parsing."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pyboy import PyBoy
from pyboy.utils import WindowEvent

from helpers import FIXTURE_DIR, read_sym, route
from labharness import Emulator, Press, Wait, _worker
from labharness.errors import EmulatorFailure
from labharness.rom import inspect_rom
from test_rom import make_rom

PROBE = (FIXTURE_DIR / "probe_sram.gb").read_bytes()
SYM = read_sym()


# ---- cartridge RAM in the trace digest ---------------------------------------------
def test_probe_rom_layout() -> None:
    info = inspect_rom(PROBE)
    assert (info.cart_type, info.ram_banks, info.ram_bank_size) == (0x1B, 4, 0x2000)


@pytest.mark.parametrize(
    ("cart_type", "code", "expected"),
    [(0x00, 0, (0, 0)), (0x19, 2, (1, 0x2000)), (0x1B, 3, (4, 0x2000)), (0x1B, 4, (16, 0x2000)),
     (0x1B, 5, (8, 0x2000)), (0x1B, 1, (1, 0x800)), (0x05, 0, (1, 0x200)), (0x06, 0, (1, 0x200))],
)
def test_ram_layout_from_header(cart_type: int, code: int, expected: tuple[int, int]) -> None:
    rom = bytearray(make_rom(cart_type))
    rom[0x149] = code
    chk = 0
    for b in rom[0x134:0x14D]:
        chk = (chk - b - 1) & 0xFF
    rom[0x14D] = chk
    info = inspect_rom(bytes(rom))
    assert (info.ram_banks, info.ram_bank_size) == expected


def test_unknown_ram_code_refused() -> None:
    from labharness.errors import RomError
    rom = bytearray(make_rom(0x1B))
    rom[0x149] = 0x09
    chk = 0
    for b in rom[0x134:0x14D]:
        chk = (chk - b - 1) & 0xFF
    rom[0x14D] = chk
    with pytest.raises(RomError, match="RAM size"):
        inspect_rom(bytes(rom))


def test_digest_covers_every_cartridge_ram_bank() -> None:
    pb = PyBoy(io.BytesIO(PROBE), window="null", sound_emulated=False, no_input=True, log_level="ERROR")
    try:
        pb.tick(5, False)
        core = _worker.Core(pb)
        with_ram, without_ram = core.digest(4, 0x2000), core.digest(0, 0)
        for bank in range(4):
            pb.memory[bank, 0xA000] = 0x40 + bank
            changed = core.digest(4, 0x2000)
            assert changed != with_ram, f"bank {bank} write not reflected"
            assert core.digest(0, 0) == without_ram  # WRAM/HRAM-only digest ignores cart RAM
            pb.memory[bank, 0xA000] = 0
            assert core.digest(4, 0x2000) == with_ram  # and the digest is a pure function of contents
        pb.memory[3, 0xBFFF] = 1                       # last byte of the last bank
        assert core.digest(4, 0x2000) != with_ram
        for bad in ((17, 0x2000), (-1, 0), (4, 0x2001), (True, 0x2000), ("4", 0x2000)):
            with pytest.raises(_worker.WorkerError):
                core.digest(*bad)  # type: ignore[arg-type]
    finally:
        pb.stop(save=False)


def test_emulator_digest_uses_the_roms_ram_layout() -> None:
    with Emulator(PROBE, 7) as emu:
        emu.execute(Wait(300))                  # let the boot ROM finish
        d1 = emu.ram_digest()
        emu.execute(Wait(10))
        assert emu.ram_digest() == d1 and emu.rom_info.ram_banks == 4  # idle ROM: stable digest


# ---- tick() reporting "emulation ended" ------------------------------------------------
def fixture_pyboy(bundle) -> PyBoy:  # type: ignore[no-untyped-def]
    return PyBoy(io.BytesIO(bundle.rom), window="null", sound_emulated=False, no_input=True, log_level="ERROR")


def test_real_quit_event_makes_tick_report_ended(bundle) -> None:  # type: ignore[no-untyped-def]
    pb = fixture_pyboy(bundle)
    try:
        pb.tick(70, False)
        pb.send_input(WindowEvent.QUIT)
        assert pb.tick(1, False) is False  # PyBoy's own signal that emulation has ended
    finally:
        pb.stop(save=False)


@pytest.mark.parametrize("phase", ["wait", "hold", "release"])
def test_worker_turns_an_ended_emulation_into_an_error(bundle, phase: str) -> None:  # type: ignore[no-untyped-def]
    pb = fixture_pyboy(bundle)
    try:
        pb.tick(70, False)
        core = _worker.Core(pb)
        real_tick = pb.tick
        calls = {"n": 0}
        fail_on = {"wait": 1, "hold": 1, "release": 2}[phase]

        class Wrapper:
            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(pb, name)

            def tick(self, count: int, render: bool) -> bool:
                calls["n"] += 1
                if calls["n"] == fail_on:
                    real_tick(1, False)
                    return False
                return real_tick(count, render)

        core.pyboy = Wrapper()
        with pytest.raises(_worker.WorkerError, match="emulation ended"):
            if phase == "wait":
                core.wait(5)
            else:
                core.press(["Right"], 2, 7)
    finally:
        pb.stop(save=False)


# ---- behavioural idle-frame boundary test ----------------------------------------------
def test_no_input_is_pending_after_any_action(bundle) -> None:  # type: ignore[no-untyped-def]
    """Observed through the game, not PyBoy internals: after an action, 30 idle frames must
    not change the game's input counter or held-button register, nor move the player."""
    from labharness import Session

    watch = [(SYM["wCurJoy"], 1), (SYM["wInputFrames"], 2), (SYM["wMapId"], 3)]  # joy, input frames, map/x/y
    with Session(bundle) as s:
        s.open()
        for action in route():
            s.execute(action)
            before = s.emulator.read_ram(watch)
            assert before[0] == 0, "a button is still held at the step boundary"
            s.emulator.execute(Wait(30))
            assert s.emulator.read_ram(watch) == before, f"residual input after {action}"


def test_input_before_the_boundary_would_be_visible(bundle) -> None:  # type: ignore[no-untyped-def]
    """Negative control: the watched registers do react to a held button."""
    with Emulator(bundle.rom, bundle.release_frames) as emu:
        emu.load_state(bundle.start_state)
        emu.execute(Press(("Right",), 60))      # held for 60 frames: input counter moves a lot
        assert int.from_bytes(emu.read_ram([(SYM["wInputFrames"], 2)]), "little") == 60


# ---- CLI ------------------------------------------------------------------------------
def labharness(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "labharness", *args], capture_output=True, text=True, timeout=180)


@pytest.fixture()
def script(tmp_path: Path) -> Path:
    from labharness.actions import action_to_dict
    p = tmp_path / "script.json"
    p.write_text(json.dumps([action_to_dict(a) for a in route()]))
    return p


def test_cli_run_matches_the_golden_trace(script: Path, tmp_path: Path) -> None:
    golden = json.loads((Path(__file__).parent / "golden" / "fixture_route.json").read_text())
    out, frames = tmp_path / "trace.json", tmp_path / "frames"
    proc = labharness("run", "--bundle", str(FIXTURE_DIR), "--script", str(script), "--out", str(out),
                      "--frames-dir", str(frames))
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["trace_sha256"] == golden["trace_sha256"] and summary["score"] == 5
    full = json.loads(out.read_text())
    assert len(full["entries"]) == golden["steps"] + 1 and full["manifest"]["release_frames"] == 7
    assert sorted(p.name for p in frames.iterdir())[0] == "0000000.png"


@pytest.mark.parametrize(
    "content",
    ['[{"tool":"press","buttons":["A"],"hold_frames":99}]', '[{"tool":"rm","path":"/"}]', "{}", "not json", "[1]"],
)
def test_cli_rejects_bad_scripts_before_emulating(tmp_path: Path, content: str) -> None:
    p = tmp_path / "bad.json"
    p.write_text(content)
    proc = labharness("run", "--bundle", str(FIXTURE_DIR), "--script", str(p))
    assert proc.returncode == 2 and proc.stdout == ""
    assert proc.stderr.startswith("error: ") and "Traceback" not in proc.stderr


def test_cli_missing_files_and_bad_bundle(tmp_path: Path, script: Path) -> None:
    assert labharness("run", "--bundle", str(tmp_path), "--script", str(script)).returncode == 2
    assert labharness("run", "--bundle", str(FIXTURE_DIR), "--script", str(tmp_path / "nope.json")).returncode == 2
    assert labharness("run", "--bundle", str(FIXTURE_DIR)).returncode == 2  # argparse usage error


def test_cli_step_cap_is_enforced(script: Path) -> None:
    proc = labharness("run", "--bundle", str(FIXTURE_DIR), "--script", str(script), "--max-steps", "3")
    assert proc.returncode == 2 and "StepLimitExceeded" in proc.stderr


def test_cli_calibrate_reports_the_bundles_release_frames() -> None:
    proc = labharness("calibrate", "--bundle", str(FIXTURE_DIR))
    assert proc.returncode == 0 and json.loads(proc.stdout)["release_frames"] == 7

