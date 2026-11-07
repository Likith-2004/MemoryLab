#!/usr/bin/env bash
# Rebuilds the test fixture ROM and symbol file with rgbds (pinned: 1.0.1, see docs/DECISIONS.md).
# Usage: build.sh [output_dir]   (default: this directory)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here}"
RGBDS="${RGBDS_DIR:-$HOME/.local/opt/rgbds-1.0.1}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
"$RGBDS/rgbasm" -o "$tmp/fixture.o" "$here/fixture.asm"
"$RGBDS/rgblink" -p 0xFF -n "$out/fixture.sym" -o "$out/fixture.gb" "$tmp/fixture.o"
"$RGBDS/rgbfix" -v -p 0xFF -t MLFIXTURE -m 0x00 -r 0x00 "$out/fixture.gb"
"$RGBDS/rgbasm" -o "$tmp/probe_sram.o" "$here/probe_sram.asm"
"$RGBDS/rgblink" -p 0xFF -o "$out/probe_sram.gb" "$tmp/probe_sram.o"
"$RGBDS/rgbfix" -v -p 0xFF -t MLPROBESRAM -m 0x1B -r 0x03 "$out/probe_sram.gb"
sha256sum "$out/fixture.gb" "$out/probe_sram.gb"
