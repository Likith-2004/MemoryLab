# Milestone 1 review record

Run: `cd packages/labharness && uv sync && .venv/bin/python -m pytest tests -q` (about 95 s; 210 tests).
Static: `.venv/bin/ruff check src tests tools` and `.venv/bin/mypy src` (strict): clean.

## Exit-criteria evidence
| Criterion | Evidence |
|---|---|
| Identical ROM + start state + scripted actions => identical RAM trace and score | `test_determinism.py`: 3 sequential runs, 4 concurrent runs, 2 separate interpreters with different `PYTHONHASHSEED`, all identical per step (WRAM+HRAM digest, telemetry, frame hash, score); matches committed golden `tests/golden/fixture_route.json`; property test over random valid scripts. |
| The comparison can fail | Negative controls: different script, 1-frame timing difference, different start state, RNG-timing dependence; plus 5 deliberate code mutations, all caught. |
| Save-state restoration | Restore in a fresh process at 4 split points reproduces the uninterrupted tail exactly (trace entries, tracker, final RAM); same-session restore after divergence; tamper/foreign/semantic/PyBoy-version/cap checks; fail-closed on RAM mismatch. |
| Button and frame limits, invalid and boundary inputs | `test_actions.py`, `test_emulator.py`, `test_session.py`. |
| RAM address validation, parsing, normalisation | `test_statemap.py` (36 rejection cases + .sym cross-check), `test_telemetry.py`. |
| Init/shutdown, failures, resources | `test_emulator.py`: idempotent close, kill on timeout, crash detection, rlimits, CPU-limit kill, hostile child commands; soak: child RSS growth < 20 MB over 300 steps with checkpoints. |
