"""Normalise raw RAM into the telemetry snapshot defined by a state map."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .constants import canonical_json
from .errors import StateParseError
from .statemap import Bitfield, IntField, StateMap, StructList, Text

UNMAPPED_CHAR = "\ufffd"


class RamView:
    """Bytes fetched from the emulator, addressable by absolute address."""

    def __init__(self, ranges: Sequence[tuple[int, int]], data: bytes) -> None:
        if sum(length for _s, length in ranges) != len(data):
            raise StateParseError("RAM data length does not match requested ranges")
        self._chunks: list[tuple[int, bytes]] = []
        pos = 0
        for start, length in ranges:
            self._chunks.append((start, data[pos : pos + length]))
            pos += length

    def read(self, addr: int, length: int) -> bytes:
        for start, chunk in self._chunks:
            if start <= addr and addr + length <= start + len(chunk):
                return chunk[addr - start : addr - start + length]
        raise StateParseError(f"address 0x{addr:04X}+{length} was not fetched")


def _uint(ram: RamView, f: IntField) -> int:
    return int.from_bytes(ram.read(f.addr, f.width), f.endian)


@dataclass(frozen=True)
class Snapshot:
    """Normalised telemetry. `extras` and `anomalies` are plain JSON-compatible data."""

    map_id: int
    x: int
    y: int
    dialog_active: bool
    milestones: int
    extras: dict[str, Any]
    anomalies: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "x": self.x,
            "y": self.y,
            "dialog_active": self.dialog_active,
            "milestones": self.milestones,
            "extras": self.extras,
            "anomalies": list(self.anomalies),
        }

    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


def parse_snapshot(sm: StateMap, ram: RamView) -> Snapshot:
    """Read every field the state map declares. Out-of-range RAM is clamped and reported
    in `anomalies` rather than hidden."""
    anomalies: list[str] = []
    extras: dict[str, Any] = {}
    for name, extra in sm.extras.items():
        if isinstance(extra, IntField):
            extras[name] = _uint(ram, extra)
        elif isinstance(extra, Bitfield):
            raw = _uint(ram, extra.value)
            extras[name] = {
                "raw": raw,
                "set": [n for bit, n in sorted(extra.names.items()) if raw >> bit & 1],
            }
        elif isinstance(extra, Text):
            extras[name] = _text(extra, ram, name, anomalies)
        else:
            extras[name] = _struct_list(extra, ram, name, anomalies)
    return Snapshot(
        map_id=_uint(ram, sm.core["map_id"]),
        x=_uint(ram, sm.core["x"]),
        y=_uint(ram, sm.core["y"]),
        dialog_active=_uint(ram, sm.core["dialog_active"]) != 0,
        milestones=_uint(ram, sm.core["milestones"]),
        extras=extras,
        anomalies=tuple(anomalies),
    )


def _text(t: Text, ram: RamView, name: str, anomalies: list[str]) -> str:
    out: list[str] = []
    for byte in ram.read(t.addr, t.max_len):
        if byte == t.terminator:
            break
        char = t.charmap.get(byte)
        if char is None:
            note = f"{name}.unmapped_byte_0x{byte:02X}"
            if note not in anomalies:  # one note per distinct byte keeps observations small
                anomalies.append(note)
            char = UNMAPPED_CHAR
        out.append(char)
    return "".join(out)


def _struct_list(s: StructList, ram: RamView, name: str, anomalies: list[str]) -> list[dict[str, int]]:
    count = _uint(ram, s.count)
    if count > s.count_max:
        anomalies.append(f"{name}.count_clamped_{count}_to_{s.count_max}")
        count = s.count_max
    items: list[dict[str, int]] = []
    for slot in range(count):
        base = s.base + slot * s.stride
        items.append(
            {sf.name: int.from_bytes(ram.read(base + sf.offset, sf.field.width), sf.field.endian) for sf in s.fields}
        )
    return items
