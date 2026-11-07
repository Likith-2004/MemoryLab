from labharness.score import ScoreTracker


def test_score_is_popcount_of_union_and_never_decreases() -> None:
    t = ScoreTracker()
    assert t.update(1, 0b0001) == [0]
    assert t.update(2, 0b0100) == [2]          # bit 0 no longer set in RAM
    assert t.seen == 0b0101 and t.score == 2   # but it still counts (OR of all seen)
    assert t.update(3, 0b0101) == []           # nothing new


def test_first_reached_step_and_tiebreak() -> None:
    t = ScoreTracker()
    t.update(4, 0b10)
    t.update(9, 0b11)
    assert t.first_step == {1: 4, 0: 9}
    assert t.tiebreak_steps == 9
    a, b = ScoreTracker(), ScoreTracker()
    a.update(5, 0b1); a.update(7, 0b11)
    b.update(5, 0b1); b.update(20, 0b11)
    assert a.rank_key() < b.rank_key()  # same score, fewer steps to last milestone wins


def test_no_milestones() -> None:
    t = ScoreTracker()
    t.update(1, 0)
    assert t.score == 0 and t.tiebreak_steps == 0


def test_multi_byte_masks_and_roundtrip() -> None:
    t = ScoreTracker()
    t.update(2, 1 << 31 | 1 << 8)
    assert t.score == 2
    assert ScoreTracker.from_dict(t.to_dict()) == t
