"""Cartridge header validation. RTC cartridges are refused in every mode."""

import hashlib
from dataclasses import dataclass

from .errors import RomError

MAX_ROM_BYTES = 8 * 1024 * 1024
# MBC3+TIMER+BATTERY ($0F) and MBC3+TIMER+RAM+BATTERY ($10): PyBoy drives these from
# wall-clock time.time(), which makes runs irreproducible.
RTC_CART_TYPES = frozenset({0x0F, 0x10})


@dataclass(frozen=True)
class RomInfo:
    sha256: str
    title: str
    cart_type: int
    cgb_flag: int
    size: int
    ram_banks: int  # external (cartridge) RAM banks; 0 when the cartridge has none
    ram_bank_size: int

    @property
    def cgb(self) -> bool:
        return self.cgb_flag in (0x80, 0xC0)


# Header byte 0x149 -> (banks, bytes per bank). MBC2 has 512 built-in half-bytes regardless.
_RAM_CODES = {0: (0, 0), 1: (1, 0x800), 2: (1, 0x2000), 3: (4, 0x2000), 4: (16, 0x2000), 5: (8, 0x2000)}
_MBC2_TYPES = frozenset({0x05, 0x06})


def inspect_rom(data: bytes) -> RomInfo:
    """Validate the header and return its facts; raises RomError on any problem."""
    if not 0x150 <= len(data) <= MAX_ROM_BYTES:
        raise RomError(f"ROM size {len(data)} outside 0x150..{MAX_ROM_BYTES}")
    size_code = data[0x148]
    if size_code > 8 or len(data) != 0x8000 << size_code:
        raise RomError("ROM size does not match header size code")
    checksum = 0
    for byte in data[0x134:0x14D]:
        checksum = (checksum - byte - 1) & 0xFF
    if checksum != data[0x14D]:
        raise RomError("header checksum mismatch")
    cart_type = data[0x147]
    if cart_type in RTC_CART_TYPES:
        raise RomError(f"RTC cartridge type 0x{cart_type:02X} is refused (non-deterministic)")
    if cart_type in _MBC2_TYPES:
        ram = (1, 0x200)
    elif data[0x149] in _RAM_CODES:
        ram = _RAM_CODES[data[0x149]]
    else:
        raise RomError(f"unknown RAM size code 0x{data[0x149]:02X}")
    title = data[0x134:0x143].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return RomInfo(
        sha256=hashlib.sha256(data).hexdigest(),
        title=title,
        cart_type=cart_type,
        cgb_flag=data[0x143],
        size=len(data),
        ram_banks=ram[0],
        ram_bank_size=ram[1],
    )
