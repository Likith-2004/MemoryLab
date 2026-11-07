"""Versioned, validated RAM state map.

A state map binds one ROM build (by sha256) to RAM addresses. Core fields are
game-agnostic; per-game extras (party, flags, dialogue text, ...) are declared here
and never hard-coded in the reader. Addresses are only ever taken from this file, and
the file is checked against the build's own symbol output by the test suite.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Literal

from .constants import HRAM, WRAM
from .errors import StateMapError

SCHEMA = 1
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HEX = re.compile(r"^0x[0-9A-Fa-f]{1,4}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
Endian = Literal["little", "big"]
CORE_FIELDS = ("map_id", "x", "y", "dialog_active", "milestones")
MAX_TEXT_LEN = 255
MAX_STRUCT_COUNT = 16


@dataclass(frozen=True)
class IntField:
    addr: int
    width: int
    endian: Endian = "little"

    @property
    def span(self) -> tuple[int, int]:
        return (self.addr, self.addr + self.width)


@dataclass(frozen=True)
class StructField:
    name: str
    offset: int
    field: IntField


@dataclass(frozen=True)
class StructList:
    count: IntField
    count_max: int
    base: int
    stride: int
    fields: tuple[StructField, ...]


@dataclass(frozen=True)
class Bitfield:
    value: IntField
    names: dict[int, str]


@dataclass(frozen=True)
class Text:
    addr: int
    max_len: int
    terminator: int
    charmap: dict[int, str]


Extra = IntField | StructList | Bitfield | Text


@dataclass(frozen=True)
class StateMap:
    version: int
    game_id: str
    rom_sha256: str
    sha256: str
    core: dict[str, IntField]
    extras: dict[str, Extra] = field(default_factory=dict)

    def spans(self) -> list[tuple[int, int]]:
        """Every byte range this map reads, as (start, end_exclusive), unmerged."""
        out: list[tuple[int, int]] = [f.span for f in self.core.values()]
        for extra in self.extras.values():
            out.extend(_extra_spans(extra))
        return out

    def read_ranges(self) -> list[tuple[int, int]]:
        """Merged (start, length) ranges to fetch from the emulator."""
        merged: list[list[int]] = []
        for start, end in sorted(self.spans()):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(s, e - s) for s, e in merged]


def _extra_spans(extra: Extra) -> list[tuple[int, int]]:
    if isinstance(extra, IntField):
        return [extra.span]
    if isinstance(extra, Bitfield):
        return [extra.value.span]
    if isinstance(extra, Text):
        return [(extra.addr, extra.addr + extra.max_len)]
    spans = [extra.count.span]
    for slot in range(extra.count_max):
        for sf in extra.fields:
            start = extra.base + slot * extra.stride + sf.offset
            spans.append((start, start + sf.field.width))
    return spans


def _fail(msg: str) -> StateMapError:
    return StateMapError(msg)


def _addr(value: object, what: str) -> int:
    if not isinstance(value, str) or not _HEX.match(value):
        raise _fail(f"{what}: address must be a hex string like '0xC000'")
    return int(value, 16)


def _int(value: object, what: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise _fail(f"{what}: must be an int in {lo}..{hi}")
    return value


def _keys(obj: object, what: str, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise _fail(f"{what}: must be an object")
    allowed = required | (optional or set())
    if not required <= set(obj) or not set(obj) <= allowed:
        raise _fail(f"{what}: keys must be {sorted(required)} (+optional {sorted(optional or [])})")
    return obj


def _int_field(obj: object, what: str, max_width: int = 4) -> IntField:
    d = _keys(obj, what, {"addr", "width"}, {"endian"})
    endian = d.get("endian", "little")
    if endian not in ("little", "big"):
        raise _fail(f"{what}: endian must be 'little' or 'big'")
    return IntField(_addr(d["addr"], what), _int(d["width"], f"{what}.width", 1, max_width), endian)


def _parse_extra(name: str, obj: object) -> Extra:
    what = f"extras.{name}"
    if not isinstance(obj, dict) or "type" not in obj:
        raise _fail(f"{what}: needs a 'type'")
    kind = obj["type"]
    if kind == "uint":
        d = _keys(obj, what, {"type", "addr", "width"}, {"endian"})
        return _int_field({k: v for k, v in d.items() if k != "type"}, what)
    if kind == "bitfield":
        d = _keys(obj, what, {"type", "addr", "width", "names"}, {"endian"})
        value = _int_field({k: v for k, v in d.items() if k not in ("type", "names")}, what)
        names_raw = d["names"]
        if not isinstance(names_raw, dict) or not names_raw:
            raise _fail(f"{what}.names: must be a non-empty object of bit -> name")
        names: dict[int, str] = {}
        for bit_s, bit_name in names_raw.items():
            if not isinstance(bit_s, str) or not bit_s.isdigit() or int(bit_s) >= value.width * 8:
                raise _fail(f"{what}.names: bad bit index {bit_s!r}")
            if not isinstance(bit_name, str) or not _NAME.match(bit_name):
                raise _fail(f"{what}.names: bad name for bit {bit_s}")
            names[int(bit_s)] = bit_name
        if len(set(names.values())) != len(names):
            raise _fail(f"{what}.names: duplicate names")
        return Bitfield(value, names)
    if kind == "text":
        d = _keys(obj, what, {"type", "addr", "max_len", "terminator", "charmap"})
        raw_map = d["charmap"]
        if not isinstance(raw_map, dict) or not raw_map:
            raise _fail(f"{what}.charmap: must be a non-empty object")
        charmap: dict[int, str] = {}
        for key, char in raw_map.items():
            if not isinstance(char, str) or len(char) != 1:
                raise _fail(f"{what}.charmap: values must be single characters")
            charmap[_addr_byte(key, f"{what}.charmap")] = char
        terminator = _int(d["terminator"], f"{what}.terminator", 0, 255)
        if terminator in charmap:
            raise _fail(f"{what}: terminator byte also appears in charmap")
        return Text(_addr(d["addr"], what), _int(d["max_len"], f"{what}.max_len", 1, MAX_TEXT_LEN), terminator, charmap)
    if kind == "struct_list":
        d = _keys(obj, what, {"type", "count", "count_max", "base", "stride", "fields"})
        count = _int_field(d["count"], f"{what}.count", max_width=1)
        stride = _int(d["stride"], f"{what}.stride", 1, 64)
        fields_raw = d["fields"]
        if not isinstance(fields_raw, dict) or not fields_raw:
            raise _fail(f"{what}.fields: must be a non-empty object")
        fields: list[StructField] = []
        for fname, fobj in fields_raw.items():
            if not _NAME.match(fname):
                raise _fail(f"{what}.fields: bad field name {fname!r}")
            fd = _keys(fobj, f"{what}.fields.{fname}", {"offset", "width"}, {"endian"})
            offset = _int(fd["offset"], f"{what}.fields.{fname}.offset", 0, 63)
            ifield = _int_field({"addr": "0x0000", "width": fd["width"], "endian": fd.get("endian", "little")}, what, 2)
            if offset + ifield.width > stride:
                raise _fail(f"{what}.fields.{fname}: extends past stride")
            fields.append(StructField(fname, offset, IntField(0, ifield.width, ifield.endian)))
        spans = sorted((f.offset, f.offset + f.field.width) for f in fields)
        if any(a[1] > b[0] for a, b in pairwise(spans)):
            raise _fail(f"{what}.fields: overlapping fields")
        return StructList(
            count, _int(d["count_max"], f"{what}.count_max", 1, MAX_STRUCT_COUNT),
            _addr(d["base"], what), stride, tuple(fields),
        )
    raise _fail(f"{what}: unknown type {kind!r}")


def _addr_byte(key: object, what: str) -> int:
    if not isinstance(key, str) or not re.match(r"^0x[0-9A-Fa-f]{2}$", key):
        raise _fail(f"{what}: keys must be byte hex strings like '0x80'")
    return int(key, 16)


def _check_regions_and_overlap(sm: StateMap) -> None:
    labelled: list[tuple[int, int, str]] = [(*f.span, f"core.{n}") for n, f in sm.core.items()]
    for name, extra in sm.extras.items():
        labelled.extend((s, e, f"extras.{name}") for s, e in _extra_spans(extra))
    for start, end, label in labelled:
        if not any(lo <= start and end <= hi for lo, hi in (WRAM, HRAM)):
            raise _fail(f"{label}: 0x{start:04X}..0x{end:04X} outside WRAM/HRAM")
    ordered = sorted(labelled)
    for (_s1, e1, l1), (s2, _e2, l2) in pairwise(ordered):
        if s2 < e1 and l1 != l2:
            raise _fail(f"overlap between {l1} and {l2} at 0x{s2:04X}")


def parse_statemap(raw: bytes) -> StateMap:
    """Parse and validate a state map file. Raises StateMapError on any defect."""
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(f"not valid JSON: {exc}") from exc
    d = _keys(doc, "statemap", {"schema", "state_map_version", "game_id", "rom_sha256", "core", "extras"})
    if d["schema"] != SCHEMA:
        raise _fail(f"unsupported schema {d['schema']!r}")
    version = _int(d["state_map_version"], "state_map_version", 1, 10**6)
    if not isinstance(d["game_id"], str) or not _NAME.match(d["game_id"]):
        raise _fail("game_id: must match [a-z][a-z0-9_]{0,31}")
    if not isinstance(d["rom_sha256"], str) or not _SHA.match(d["rom_sha256"]):
        raise _fail("rom_sha256: must be 64 lowercase hex chars")
    core_raw = _keys(d["core"], "core", set(CORE_FIELDS))
    core = {name: _int_field(core_raw[name], f"core.{name}", 4 if name == "milestones" else 2) for name in CORE_FIELDS}
    if core["dialog_active"].width != 1:
        raise _fail("core.dialog_active: width must be 1")
    extras_raw = d["extras"]
    if not isinstance(extras_raw, dict):
        raise _fail("extras: must be an object")
    extras: dict[str, Extra] = {}
    for name, obj in extras_raw.items():
        if not isinstance(name, str) or not _NAME.match(name) or name in CORE_FIELDS:
            raise _fail(f"extras: bad or reserved name {name!r}")
        extras[name] = _parse_extra(name, obj)
    sm = StateMap(version, d["game_id"], d["rom_sha256"], hashlib.sha256(raw).hexdigest(), core, extras)
    _check_regions_and_overlap(sm)
    return sm
