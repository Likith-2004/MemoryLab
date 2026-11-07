import json

import pytest

from helpers import FIXTURE_DIR
from labharness.errors import StateParseError
from labharness.statemap import parse_statemap
from labharness.telemetry import UNMAPPED_CHAR, RamView, parse_snapshot

BASE = 0xC000


def sm_from(doc: dict) -> object:  # type: ignore[type-arg]
    return parse_statemap(json.dumps(doc).encode())


FIXTURE_MAP = parse_statemap((FIXTURE_DIR / "fixture.statemap.json").read_bytes())


def view_for(sm, mem: bytearray) -> RamView:  # type: ignore[no-untyped-def]
    ranges = sm.read_ranges()
    return RamView(ranges, b"".join(bytes(mem[s - BASE : s - BASE + n]) for s, n in ranges))


def blank() -> bytearray:
    return bytearray(0x1000)


def test_core_fields_and_little_endian_milestones() -> None:
    mem = blank()
    mem[0x00], mem[0x01], mem[0x02], mem[0x03] = 2, 7, 9, 1
    mem[0x04], mem[0x05] = 0x09, 0x01     # 0x0109
    snap = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    assert (snap.map_id, snap.x, snap.y, snap.dialog_active, snap.milestones) == (2, 7, 9, True, 0x109)


def test_dialog_active_is_any_nonzero() -> None:
    for value, expected in ((0, False), (1, True), (255, True)):
        mem = blank(); mem[3] = value
        assert parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem)).dialog_active is expected


def test_party_parsing_and_count_clamp_is_reported() -> None:
    mem = blank()
    mem[0x28] = 0x50  # empty, terminated dialogue buffer
    mem[0x0E] = 2
    mem[0x0F:0x17] = bytes([1, 5, 20, 20, 4, 3, 12, 12])
    snap = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    assert snap.extras["party"] == [
        {"species": 1, "level": 5, "hp": 20, "max_hp": 20},
        {"species": 4, "level": 3, "hp": 12, "max_hp": 12},
    ]
    assert snap.anomalies == ()
    mem[0x0E] = 200  # garbage count: clamp, and say so
    snap = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    assert len(snap.extras["party"]) == 6
    assert snap.anomalies == ("party.count_clamped_200_to_6",)


def test_empty_party() -> None:
    assert parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, blank())).extras["party"] == []


def test_bitfield() -> None:
    mem = blank(); mem[0x27] = 0b1000_0001
    flags = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem)).extras["flags"]
    assert flags == {"raw": 0x81, "set": ["pressed_a"]}


def test_text_charmap_terminator_and_unmapped() -> None:
    mem = blank()
    mem[0x28:0x30] = bytes([0x87, 0x88, 0x7F, 0x99, 0x50, 0x80, 0x80, 0x80])  # "HI Z@AAA"
    snap = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    assert snap.extras["dialog_text"] == "HI Z"
    mem[0x28:0x2B] = bytes([0x80, 0x01, 0x81])  # unmapped byte in the middle
    snap = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    assert snap.extras["dialog_text"].startswith(f"A{UNMAPPED_CHAR}B")
    assert any("unmapped_byte_0x01" in a for a in snap.anomalies)
    mem[0x28:0x38] = bytes([0x01] * 16)
    snap = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    assert snap.anomalies == ("dialog_text.unmapped_byte_0x01",)  # deduplicated


def test_text_without_terminator_is_bounded() -> None:
    mem = blank(); mem[0x28:0x38] = bytes([0x80] * 16)
    assert parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem)).extras["dialog_text"] == "A" * 16


def test_big_endian_field() -> None:
    doc = json.loads((FIXTURE_DIR / "fixture.statemap.json").read_text())
    doc["core"]["milestones"]["endian"] = "big"
    sm = sm_from(doc)
    mem = blank(); mem[4], mem[5] = 0x01, 0x02
    assert parse_snapshot(sm, view_for(sm, mem)).milestones == 0x0102


def test_snapshot_hash_is_stable_and_sensitive() -> None:
    mem = blank(); mem[1] = 4
    a = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    b = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    mem[1] = 5
    c = parse_snapshot(FIXTURE_MAP, view_for(FIXTURE_MAP, mem))
    assert a.sha256() == b.sha256() != c.sha256()


def test_ramview_rejects_unfetched_and_mismatched_data() -> None:
    view = RamView([(0xC000, 4)], b"\x00" * 4)
    assert view.read(0xC001, 2) == b"\x00\x00"
    with pytest.raises(StateParseError):
        view.read(0xC003, 2)
    with pytest.raises(StateParseError):
        view.read(0xD000, 1)
    with pytest.raises(StateParseError):
        RamView([(0xC000, 4)], b"\x00" * 3)
