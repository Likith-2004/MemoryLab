# labharness

Deterministic emulator layer of the Memory Lab harness: PyBoy 2.9.0 headless in a child
process, validated button/wait actions, a versioned RAM state map, per-step telemetry and RAM
digests, scored traces, step-boundary checkpoints.

## Usage notes (read these)

- **Linux only.** Tested on Linux (x86_64, Python 3.14). The child process uses `resource`
  rlimits and `/proc`-based tests; macOS/Windows are untested and unsupported.
- **Always guard your entry point.** The emulator child is *spawned* (not forked), which
  re-imports the main module. Scripts that start an `Emulator`/`Session` must wrap their
  top level in `if __name__ == "__main__":` or they will recurse. `python -m labharness`
  and the `labharness` console script are already guarded.
- **Not thread-safe.** One `Emulator`/`Session` per caller; run several in parallel by
  creating several (each is its own process). Never share one between threads.
- **No host disk writes.** The ROM is passed as bytes; PyBoy cannot create `.ram`/`.rtc`
  files. RTC cartridges are refused.
- **Game text is untrusted.** See `docs/SECURITY.md` (U-1).
- Resource limits: `RLIMIT_AS` is a virtual-address cap (floor 768 MiB: 512 MiB cannot import
  PyBoy). The container limit (M5) is the real memory bound.

## Commands

```bash
labharness run --bundle packages/games/fixture --script script.json --out trace.json
labharness calibrate --bundle packages/games/fixture      # measure release_frames
```

`script.json` is a list such as
`[{"tool":"press","buttons":["Right","Down"],"hold_frames":2},{"tool":"wait","frames":30}]`.
Invalid scripts exit with code 2 and a one-line error before any emulation starts.

## Game bundles

`bundle.json` pins ROM, start state, state map (sha256) and `release_frames` (measured per
game with `labharness calibrate`). Changing any of them changes the manifest hash.
