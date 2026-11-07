"""Screenshot policy and deterministic frame scaling/encoding."""

import hashlib
import io
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from PIL import Image

from .telemetry import Snapshot

Mode = Literal["never", "every_k", "every_k_or_change"]


@dataclass(frozen=True)
class ScreenshotPolicy:
    mode: Mode = "every_k_or_change"
    k: int = 5
    frame_scale: int = 2

    def __post_init__(self) -> None:
        if self.mode not in ("never", "every_k", "every_k_or_change"):
            raise ValueError("unknown screenshot mode")
        if not 1 <= self.k <= 10_000:
            raise ValueError("k must be in 1..10000")
        if not 1 <= self.frame_scale <= 8:
            raise ValueError("frame_scale must be in 1..8")

    def should_capture(self, step: int, prev: Snapshot | None, cur: Snapshot) -> bool:
        """Capture every K steps (step 0 included) and, in `every_k_or_change` mode, on a
        map or dialogue-state change."""
        if self.mode == "never":
            return False
        if step % self.k == 0:
            return True
        if self.mode == "every_k_or_change" and prev is not None:
            return prev.map_id != cur.map_id or prev.dialog_active != cur.dialog_active
        return False


def scale_nearest(rgb: "np.ndarray[Any, Any]", scale: int) -> "np.ndarray[Any, Any]":
    return np.repeat(np.repeat(rgb, scale, axis=0), scale, axis=1)


def encode_png(rgb: "np.ndarray[Any, Any]") -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb), "RGB").save(buf, format="PNG", compress_level=6, optimize=False)
    return buf.getvalue()


def frame_sha256(rgb: "np.ndarray[Any, Any]") -> str:
    """Hash of the raw (unscaled) pixels, stable across PNG encoder versions."""
    return hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).hexdigest()
