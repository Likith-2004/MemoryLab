import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from labharness import Press, Wait, parse_action
from labharness.actions import action_to_dict
from labharness.actions import checked_cost
from labharness.constants import ACTION_FRAMES_MAX, BUTTONS
from labharness.errors import InvalidAction


def test_press_valid_and_frame_cost() -> None:
    p = Press(("A", "Up"), 10)
    assert p.cost(7) == 2 * (10 + 7) and p.cost(1) == 2 * 11


@pytest.mark.parametrize("hold", [1, 60])
def test_hold_boundaries_accepted(hold: int) -> None:
    Press(("A",), hold)


@pytest.mark.parametrize("hold", [0, -1, 61, 1000, True, 1.0, "5", None])
def test_hold_out_of_range_or_wrong_type_rejected(hold: object) -> None:
    with pytest.raises(InvalidAction):
        Press(("A",), hold)  # type: ignore[arg-type]


def test_button_count_boundaries() -> None:
    Press(("A",) * 8, 1)
    with pytest.raises(InvalidAction):
        Press((), 1)
    with pytest.raises(InvalidAction):
        Press(("A",) * 9, 1)


@pytest.mark.parametrize("button", ["a", "UP", "up", "", "Turbo", " A", "A\n", 1, None])
def test_unknown_buttons_rejected(button: object) -> None:
    with pytest.raises(InvalidAction):
        Press((button,), 1)  # type: ignore[arg-type]


def test_total_frames_cap_depends_on_the_games_release_frames() -> None:
    p = Press(("A",) * 8, 60)
    assert checked_cost(p, 7) == 8 * 67 <= ACTION_FRAMES_MAX
    assert checked_cost(p, 15) == 600                      # boundary: allowed
    with pytest.raises(InvalidAction, match="600"):
        checked_cost(p, 16)                                # 608 frames
    assert checked_cost(Wait(600), 60) == 600              # waits ignore release frames


@pytest.mark.parametrize("n", [1, 600])
def test_wait_boundaries_accepted(n: int) -> None:
    assert Wait(n).cost(7) == n


@pytest.mark.parametrize("n", [0, -5, 601, True, 2.5, "9", None])
def test_wait_rejected(n: object) -> None:
    with pytest.raises(InvalidAction):
        Wait(n)  # type: ignore[arg-type]


def test_parse_roundtrip() -> None:
    for action in (Press(("Start", "Select"), 3), Wait(42)):
        assert parse_action(action_to_dict(action)) == action


@pytest.mark.parametrize(
    "raw",
    [
        None, [], "press", 7,
        {}, {"tool": "run"}, {"tool": "press"}, {"tool": "press", "buttons": "A", "hold_frames": 1},
        {"tool": "press", "buttons": ["A"], "hold_frames": 1, "extra": 1},
        {"tool": "wait", "frames": 5, "x": 1}, {"tool": "wait"},
        {"tool": ["press"], "buttons": ["A"], "hold_frames": 1},
        {"tool": "press", "buttons": [["A"]], "hold_frames": 1},
        {"tool": "mem_write", "path": "a", "content": "b"},
    ],
)
def test_parse_rejects_malformed(raw: object) -> None:
    with pytest.raises(InvalidAction):
        parse_action(raw)


json_like = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=20),
    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=12), c, max_size=4),
    max_leaves=12,
)


@settings(max_examples=300, deadline=None)
@given(json_like)
def test_parse_never_raises_anything_but_invalid_action(raw: object) -> None:
    try:
        parse_action(raw)
    except InvalidAction:
        pass


@settings(max_examples=200, deadline=None)
@given(
    st.lists(st.sampled_from(BUTTONS), min_size=1, max_size=8),
    st.integers(min_value=1, max_value=60),
)
def test_every_valid_press_stays_under_frame_cap(buttons: list[str], hold: int) -> None:
    assert Press(tuple(buttons), hold).cost(7) <= ACTION_FRAMES_MAX
