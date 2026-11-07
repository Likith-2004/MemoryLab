# Memory Lab test fixture (NOT a game)

A 32 KiB Game Boy ROM (ROM-only cartridge, no RAM/battery/RTC) used only to test the
emulator harness. It is labelled `"fixture": true` in `bundle.json`, is never listed in
`games`, and is never offered to visitors. It exists so the harness can be tested without
any third-party ROM; the public lab game is a separate design track (docs/DECISIONS.md).

| File | What |
|---|---|
| `fixture.asm` | RGBDS source (CC0) |
| `build.sh` | rebuilds `fixture.gb` + `fixture.sym` with pinned rgbds 1.0.1 (reproducible; tested) |
| `fixture.sym` | the build's own symbol file: the source of truth for every address |
| `fixture.statemap.json` | state map v1, generated from `fixture.sym` by `labharness/tools/make_fixture_assets.py` |
| `fixture.state` | fixed start state (cold boot + init, PyBoy 2.9.0 format) |
| `bundle.json` | sha256 pins for ROM, state, state map, and the PyBoy version of the state |

Game behaviour: 16x16 grid, two maps. D-pad moves one tile per 8-frame walk lock (held
direction repeats). Start opens a dialogue (map-specific text), B closes it, A sets a flag.
Milestones (16-bit): bit0 reach (3,3) on map 1; bit1 enter map 2; bit2 open dialogue on
map 2; bit3 reach (5,5) on map 2; bit8 reach y=15 on map 2. Moving on map 2 lowers the
lead creature's HP. A per-frame RNG mixes DIV on every new button press, so reproducible
traces depend on exact input timing.

Regenerate (only when the ROM changes; this changes every hash and the golden trace):
`./build.sh && (cd ../../labharness && .venv/bin/python -I tools/make_fixture_assets.py ../games/fixture && .venv/bin/python -I tests/regen_golden.py)`
