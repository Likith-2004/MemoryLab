import copy
import json
from itertools import pairwise
from typing import Any

import pytest

from helpers import FIXTURE_DIR, read_sym
from labharness.errors import StateMapError
from labharness.statemap import IntField, parse_statemap

GOOD: dict[str, Any] = json.loads((FIXTURE_DIR / "fixture.statemap.json").read_text())


def parse(doc: Any) -> Any:
    return parse_statemap(json.dumps(doc).encode())


def mutate(fn: Any) -> dict[str, Any]:
    doc = copy.deepcopy(GOOD)
    fn(doc)
    return doc


def test_committed_map_parses_and_matches_build_symbols() -> None:
    """Every address in the committed map must equal the label the ROM build emitted."""
    sm = parse(GOOD)
    sym = read_sym()
    assert sm.core["map_id"].addr == sym["wMapId"]
    assert sm.core["x"].addr == sym["wX"] and sm.core["y"].addr == sym["wY"]
    assert sm.core["dialog_active"].addr == sym["wDialog"]
    assert sm.core["milestones"] == IntField(sym["wMilestones"], 2, "little")
    assert sm.extras["party"].count.addr == sym["wPartyCount"]  # type: ignore[union-attr]
    assert sm.extras["party"].base == sym["wParty"]  # type: ignore[union-attr]
    assert sm.extras["flags"].value.addr == sym["wFlags"]  # type: ignore[union-attr]
    assert sm.extras["dialog_text"].addr == sym["wDialogText"]  # type: ignore[union-attr]
    # layout facts the map relies on: party stride 4 x 6 slots ends exactly at wFlags
    assert sym["wParty"] + 6 * 4 == sym["wFlags"]
    assert sym["wDialogText"] + 16 == sym["wInitDone"]


def test_read_ranges_are_merged_and_inside_wram() -> None:
    sm = parse(GOOD)
    ranges = sm.read_ranges()
    assert ranges == sorted(ranges)
    for (s1, l1), (s2, _l2) in pairwise(ranges):
        assert s1 + l1 < s2  # merged: no touching/overlapping neighbours
    assert all(0xC000 <= s and s + n <= 0xE000 for s, n in ranges)


BAD_CASES = {
    "not_wram": lambda d: d["core"]["x"].update(addr="0x8000"),
    "rom_region": lambda d: d["core"]["x"].update(addr="0x4000"),
    "straddles_end": lambda d: d["core"]["milestones"].update(addr="0xDFFF"),
    "decimal_addr": lambda d: d["core"]["x"].update(addr=49153),
    "bad_hex": lambda d: d["core"]["x"].update(addr="0xZZZZ"),
    "zero_width": lambda d: d["core"]["x"].update(width=0),
    "huge_width": lambda d: d["core"]["milestones"].update(width=5),
    "bool_width": lambda d: d["core"]["x"].update(width=True),
    "overlap": lambda d: d["core"]["y"].update(addr=d["core"]["x"]["addr"]),
    "extra_overlaps_core": lambda d: d["extras"]["flags"].update(addr=d["core"]["x"]["addr"]),
    "missing_core_field": lambda d: d["core"].pop("y"),
    "unknown_core_field": lambda d: d["core"].update(bonus={"addr": "0xC100", "width": 1}),
    "unknown_key": lambda d: d["core"]["x"].update(signed=True),
    "bad_endian": lambda d: d["core"]["x"].update(endian="middle"),
    "dialog_width_2": lambda d: d["core"]["dialog_active"].update(width=2),
    "bad_schema": lambda d: d.update(schema=2),
    "bad_version": lambda d: d.update(state_map_version=0),
    "bad_rom_sha": lambda d: d.update(rom_sha256="XYZ"),
    "bad_game_id": lambda d: d.update(game_id="Bad Id"),
    "extra_reserved_name": lambda d: d["extras"].update(x=d["extras"]["flags"]),
    "extra_unknown_type": lambda d: d["extras"]["flags"].update(type="blob"),
    "struct_field_past_stride": lambda d: d["extras"]["party"]["fields"]["max_hp"].update(offset=4),
    "struct_overlapping_fields": lambda d: d["extras"]["party"]["fields"]["hp"].update(offset=1),
    "struct_count_max_zero": lambda d: d["extras"]["party"].update(count_max=0),
    "struct_count_max_huge": lambda d: d["extras"]["party"].update(count_max=17),
    "struct_extent_leaves_wram": lambda d: d["extras"]["party"].update(base="0xDFF0"),
    "struct_stride_zero": lambda d: d["extras"]["party"].update(stride=0),
    "bitfield_bad_bit": lambda d: d["extras"]["flags"]["names"].update({"8": "too_high"}),
    "bitfield_dup_names": lambda d: d["extras"]["flags"]["names"].update({"1": "pressed_a"}),
    "bitfield_empty": lambda d: d["extras"]["flags"].update(names={}),
    "text_terminator_in_charmap": lambda d: d["extras"]["dialog_text"].update(terminator=0x80),
    "text_multichar": lambda d: d["extras"]["dialog_text"]["charmap"].update({"0x80": "AB"}),
    "text_bad_key": lambda d: d["extras"]["dialog_text"]["charmap"].update({"128": "A"}),
    "text_len_zero": lambda d: d["extras"]["dialog_text"].update(max_len=0),
    "text_len_huge": lambda d: d["extras"]["dialog_text"].update(max_len=256),
}


@pytest.mark.parametrize("name", sorted(BAD_CASES))
def test_invalid_maps_rejected(name: str) -> None:
    with pytest.raises(StateMapError):
        parse(mutate(BAD_CASES[name]))


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b"\xff\xfe", b'{"schema":1}'])
def test_garbage_rejected(raw: bytes) -> None:
    with pytest.raises(StateMapError):
        parse_statemap(raw)


def test_hram_is_allowed() -> None:
    doc = mutate(lambda d: d["core"]["x"].update(addr="0xFF90"))
    assert parse(doc).core["x"].addr == 0xFF90
