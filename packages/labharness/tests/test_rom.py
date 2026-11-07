import pytest

from helpers import FIXTURE_DIR
from labharness.errors import RomError
from labharness.rom import inspect_rom


def make_rom(cart_type: int = 0x00, size_code: int = 0, cgb: int = 0x00) -> bytes:
    rom = bytearray(0x8000 << size_code)
    rom[0x134:0x13B] = b"TESTROM"
    rom[0x143] = cgb
    rom[0x147] = cart_type
    rom[0x148] = size_code
    chk = 0
    for b in rom[0x134:0x14D]:
        chk = (chk - b - 1) & 0xFF
    rom[0x14D] = chk
    return bytes(rom)


def test_fixture_rom_accepted() -> None:
    info = inspect_rom((FIXTURE_DIR / "fixture.gb").read_bytes())
    assert info.title == "MLFIXTURE" and info.cart_type == 0 and not info.cgb


@pytest.mark.parametrize("cart_type", [0x0F, 0x10])
def test_rtc_cartridges_refused(cart_type: int) -> None:
    with pytest.raises(RomError, match="RTC"):
        inspect_rom(make_rom(cart_type))


def test_non_rtc_mbc3_allowed() -> None:
    inspect_rom(make_rom(0x11))  # MBC3 without timer


def test_bad_checksum_refused() -> None:
    rom = bytearray(make_rom())
    rom[0x14D] ^= 0xFF
    with pytest.raises(RomError, match="checksum"):
        inspect_rom(bytes(rom))


@pytest.mark.parametrize("data", [b"", b"\x00" * 0x14F, b"\x00" * (9 * 1024 * 1024)])
def test_bad_sizes_refused(data: bytes) -> None:
    with pytest.raises(RomError):
        inspect_rom(data)


def test_size_code_mismatch_refused() -> None:
    rom = bytearray(make_rom())
    rom[0x148] = 1  # claims 64 KiB, is 32 KiB
    with pytest.raises(RomError):
        inspect_rom(bytes(rom))
