"""Progress score: popcount of the OR of every milestone mask seen, with the step each
milestone was first reached as the tiebreak."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScoreTracker:
    seen: int = 0
    first_step: dict[int, int] = field(default_factory=dict)

    def update(self, step: int, milestones: int) -> list[int]:
        """Fold in the milestone mask observed at `step`; returns newly reached bits."""
        new_bits = [b for b in range(milestones.bit_length()) if milestones >> b & 1 and not self.seen >> b & 1]
        for bit in new_bits:
            self.first_step[bit] = step
        self.seen |= milestones
        return new_bits

    @property
    def score(self) -> int:
        return self.seen.bit_count()

    @property
    def tiebreak_steps(self) -> int:
        """Steps to the last milestone reached (lower is better); 0 when none reached."""
        return max(self.first_step.values(), default=0)

    def rank_key(self) -> tuple[int, int]:
        return (-self.score, self.tiebreak_steps)

    def to_dict(self) -> dict[str, Any]:
        return {"seen": self.seen, "first_step": {str(b): s for b, s in sorted(self.first_step.items())}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreTracker":
        return cls(int(data["seen"]), {int(b): int(s) for b, s in data["first_step"].items()})
