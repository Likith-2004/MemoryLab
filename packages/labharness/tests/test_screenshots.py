import io

import numpy as np
import pytest
from PIL import Image

from labharness.screenshots import ScreenshotPolicy, encode_png, frame_sha256, scale_nearest
from labharness.telemetry import Snapshot


def snap(map_id: int = 1, dialog: bool = False) -> Snapshot:
    return Snapshot(map_id, 0, 0, dialog, 0, {}, ())


def test_every_k_includes_step_zero_and_multiples() -> None:
    p = ScreenshotPolicy("every_k", k=5)
    s = snap()
    assert [n for n in range(12) if p.should_capture(n, s, s)] == [0, 5, 10]


def test_change_triggers_only_in_change_mode() -> None:
    change, plain = ScreenshotPolicy("every_k_or_change", k=100), ScreenshotPolicy("every_k", k=100)
    assert change.should_capture(3, snap(1), snap(2)) and not plain.should_capture(3, snap(1), snap(2))
    assert change.should_capture(3, snap(1, False), snap(1, True))
    assert not change.should_capture(3, snap(1), snap(1))
    assert not change.should_capture(3, None, snap(1))


def test_never() -> None:
    p = ScreenshotPolicy("never")
    assert not p.should_capture(0, None, snap())


@pytest.mark.parametrize("kwargs", [{"k": 0}, {"k": 10_001}, {"frame_scale": 0}, {"frame_scale": 9}, {"mode": "always"}])
def test_policy_validation(kwargs: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        ScreenshotPolicy(**kwargs)


def test_nearest_neighbour_scaling_is_exact() -> None:
    rgb = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    out = scale_nearest(rgb, 3)
    assert out.shape == (6, 9, 3)
    assert set(np.unique(out)) == set(np.unique(rgb))  # no interpolated values
    assert (out[:3, :3] == rgb[0, 0]).all() and (out[3:, 6:] == rgb[1, 2]).all()


def test_png_is_deterministic_and_lossless() -> None:
    rgb = np.random.default_rng(1).integers(0, 255, (144, 160, 3), dtype=np.uint8)
    a, b = encode_png(scale_nearest(rgb, 2)), encode_png(scale_nearest(rgb, 2))
    assert a == b and a.startswith(b"\x89PNG")
    decoded = np.array(Image.open(io.BytesIO(a)))
    assert decoded.shape == (288, 320, 3) and (decoded[::2, ::2] == rgb).all()
    assert frame_sha256(rgb) == frame_sha256(rgb.copy())
