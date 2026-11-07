# Third-party components (Milestone 1)

| Component | Version | Licence | Use |
|---|---|---|---|
| PyBoy | 2.9.0 | LGPL-3.0 (`LICENSE.md` in the PyBoy repo; the PyPI metadata field is empty) | Imported unmodified as a pip dependency; never vendored or patched. Container images must ship the licence text and attribution and keep it replaceable (installed via pip). Legal confirmation before public launch is recommended. |
| pysdl2 / pysdl2-dll | 0.9.17 / 2.32.10 | **not yet audited** | PyBoy dependency (unused: null window). Licence to be confirmed from the packages before launch. |
| numpy, Pillow | see `uv.lock` | **not yet audited** (expected permissive) | Frame handling. Confirm before launch. |
| rgbds | 1.0.1 | MIT | Build tool for the fixture ROM only; not shipped. |
| Fixture ROM | n/a | CC0-1.0 (authored for Memory Lab) | Tests only. |
