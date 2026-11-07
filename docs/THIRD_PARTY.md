# Third-party components and licence audit

Audited 2026-11-08 from the installed distributions' own metadata and licence files
(`scripts/licence_audit.py`, run in the locked venv). Re-run whenever `uv.lock` changes and
update this file. Dependencies are added only if the licence is permissive or weak copyleft
and acceptable for a hosted service; anything stronger (GPL/AGPL/SSPL/BUSL) is refused.

## Runtime dependencies (Milestone 1; later milestones append here)

| Component | Version | Licence (from metadata) | Notes |
|---|---|---|---|
| PyBoy | 2.9.0 | LGPL-3.0-only | Imported unmodified as a pip dependency, never vendored or patched. Images must ship the licence text and attribution and keep PyBoy replaceable (plain pip install). Server-side use does not distribute it to visitors, but images/bundles we share would; get legal sign-off before any public launch (see LAUNCH.md). |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | |
| Pillow | 13.0.0 | MIT-CMU (HPND-style) | |
| PySDL2 | 0.9.17 | Public Domain / zlib | PyBoy dependency; unused at runtime (null window). |
| pysdl2-dll | 2.32.10 | MPL-2.0 (wrapper); bundles SDL2 (zlib) and SDL2_gfx/image/mixer/ttf plus avif, dav1d, tiff, webp, ogg, opus, wavpack, xmp, gme | MPL-2.0 is file-level copyleft; we do not modify it. Its many bundled codecs are unneeded with a null window and enlarge the attack surface: the sandbox image (M5) should prune unused libraries if PyBoy still starts, otherwise record as a known limitation. |

## Development-only dependencies (not shipped)

| Component | Version | Licence |
|---|---|---|
| pytest / pluggy / iniconfig / packaging | 9.2.0 / 1.7.0 / 2.3.1 / 26.3 | MIT / MIT / MIT / Apache-2.0 OR BSD-2-Clause |
| hypothesis | 6.170.15 | MPL-2.0 (+ sortedcontainers 2.4.0, Apache-2.0) |
| ruff | 0.17.4 | MIT |
| mypy (+ mypy_extensions, librt, ast_serialize, pathspec) | 2.4.1 | MIT (pathspec: MPL-2.0 per classifier) |
| Pygments, typing_extensions | 2.21.0 / 4.16.0 | BSD-2-Clause / PSF-2.0 |

## Tools and assets

| Item | Licence | Notes |
|---|---|---|
| rgbds 1.0.1 | MIT | Build tool for the fixture ROMs only; not shipped. Installed from the upstream release tarball, sha256 `80a5cad8dae27e24e46a93041352c47cadbc165103983f41c2b3082c42f6dad9` verified. |
| Fixture ROMs (`packages/games/fixture`) | CC0-1.0 | Authored for Memory Lab. Test-only; never offered to visitors. |
