"""Wire format between the parent and the emulator child: 4-byte header length, JSON
header, raw payload. JSON+bytes (not pickle) so a compromised child cannot make the
parent execute code while decoding its replies."""

import json
import struct
from typing import Any

MAX_MESSAGE_BYTES = 32 * 1024 * 1024


def encode(header: dict[str, Any], payload: bytes = b"") -> bytes:
    head = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(head)) + head + payload


def decode(raw: bytes) -> tuple[dict[str, Any], bytes]:
    if len(raw) < 4 or len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("bad message size")
    (n,) = struct.unpack(">I", raw[:4])
    if n > len(raw) - 4:
        raise ValueError("bad header length")
    header = json.loads(raw[4 : 4 + n])
    if not isinstance(header, dict):
        raise ValueError("header must be an object")  # noqa: TRY004 - callers catch ValueError
    return header, raw[4 + n :]
