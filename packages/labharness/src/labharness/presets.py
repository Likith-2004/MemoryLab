"""Instruction presets: immutable, versioned text blobs labelled as adaptations.

These texts were written by Memory Lab from public descriptions of memory-instruction
styles. They are NOT Anthropic's prompts and must never be presented as such. A shipped
preset's text never changes: edits require a new version (a test pins each text hash).
"""

import hashlib
from dataclasses import dataclass

ADAPTATION_LABEL = "adaptation: written by Memory Lab, not Anthropic's prompt"
CUSTOM_LABEL = "user-written instructions"
MAX_CUSTOM_CHARS = 4000


@dataclass(frozen=True)
class Preset:
    id: str
    version: int
    text: str
    label: str
    is_adaptation: bool

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


_NOTES_INTRO = (
    "You have a private notes folder that lasts for the whole run. Use the tools mem_list, mem_read, "
    "mem_write, mem_delete and mem_search. Your conversation history is short and older turns are "
    "forgotten, but your notes are not."
)

_TEXTS = {
    "none": "",
    "general": (
        f"{_NOTES_INTRO}\n\nWrite down what will help you later: your goal, where things are, how mechanics "
        "work, and what to do next. Keep notes short and accurate. When a note turns out to be wrong, fix "
        "or delete it. Read your notes when you begin and whenever you feel unsure."
    ),
    "failures_only": (
        f"{_NOTES_INTRO}\n\nRecord only failures and dead ends: things you tried that did not work, places "
        "that were blocked, and mistakes that cost you time. Do not record successes or plans. Before you "
        "try something, search your notes so you do not repeat a failure you already recorded."
    ),
    "read_first_test_theories": (
        f"{_NOTES_INTRO}\n\nRead your notes before you act: mem_list first, then mem_read the files that "
        "matter. When you form a theory about how the game works, write it as a short note marked "
        "'theory', test it by playing, then change it to 'confirmed' or 'refuted'. Prefer specific, "
        "testable notes and delete refuted theories."
    ),
}

PRESETS: dict[str, Preset] = {
    f"{pid}@1": Preset(pid, 1, text, ADAPTATION_LABEL, True) for pid, text in _TEXTS.items()
}


def get_preset(ref: str) -> Preset:
    try:
        return PRESETS[ref]
    except KeyError:
        raise ValueError("unknown preset") from None


def custom_preset(text: str) -> Preset:
    """User-written instructions (Demo mode only; the API layer enforces the mode)."""
    if not isinstance(text, str) or len(text) > MAX_CUSTOM_CHARS:
        raise ValueError(f"custom preset must be text of at most {MAX_CUSTOM_CHARS} chars")
    if any((ord(c) < 0x20 and c not in "\n\t") or ord(c) == 0x7F or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError("custom preset contains control characters")
    return Preset("custom", 0, text, CUSTOM_LABEL, False)
