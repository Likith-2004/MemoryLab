# GB Studio symbol-export path (verification only; design track is separate)

Source inspected: `chrismaltby/gb-studio` @ `eb6aa8a7` (2026-10-21), package version 4.3.2, MIT.
Inspected by reading source in an isolated temp clone; **no GB Studio build was run**.

Verified from source:
- A headless CLI exists: `gb-studio-cli make:rom <projectFile> <destination.gb>` (also `export`, `make:pocket`, `make:web`) (`src/apps/gb-studio-cli/gb-studio-cli.ts`).
- The build links with GBDK-2020 using `-Wl-j -Wl-m -Wl-w` (`src/lib/compiler/buildMakeScript.ts`), and the build manifest names two artifacts next to the ROM: `<rom>.map` and `<rom>.noi` (`buildManifest.ts`, `build/rom/`). These are SDCC/GBDK linker outputs, **not** RGBDS `.sym` files, so a different parser is needed for the real game.
- Project variables get consecutive slots in one global variable block (`globalVariableOffsets` in `generateGBVMData.ts`; offset = running sum of variable sizes, in the order of the variable alias table). So a RAM address for a named project variable = address of the global variable block (from the `.map`/`.noi`) + `offset * word size`.

NOT verified (needs an actual build, to be done in the Option A track):
- The exact linker symbol name and address of the global variable block, and whether variables are 16-bit words in memory (expected, unconfirmed).
- That the variable ordering is stable across rebuilds for one project (must be pinned by the state map + ROM sha256, as with the fixture).
- Byte order and where GB Studio keeps map id / player position (engine fields vs. project variables).

Consequence for the harness: the state map format already binds addresses to a ROM sha256 and supports `endian`, `width` and arbitrary WRAM addresses, so no format change is expected; a `.map`/`.noi` -> state-map generator (analogous to `tools/make_fixture_assets.py`) is the missing piece.
