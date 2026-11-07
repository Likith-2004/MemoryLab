"""Scripted provider for tests and offline runs: no network, fully deterministic."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .base import (
    ProviderError,
    ProviderRequest,
    ProviderResult,
    TextBlock,
    ToolCall,
    ToolUseBlock,
    Usage,
)

Script = Iterable["ScriptedStep | ProviderError | Callable[[ProviderRequest], ScriptedStep]"]


@dataclass(frozen=True)
class ScriptedStep:
    calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    text: str = ""
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=100, output_tokens=20))
    stop_reason: str = "tool_use"


class FakeProvider:
    """Returns the next scripted step each call; raises scripted ProviderErrors. Records every
    request so tests can assert exactly what the model would have seen."""

    name = "fake"

    def __init__(self, script: Script, model_id: str = "fake-model") -> None:
        self.model_id = model_id
        self._script = iter(script)
        self.requests: list[ProviderRequest] = []
        self._n = 0

    def step(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        try:
            item = next(self._script)
        except StopIteration:
            raise ProviderError("fake script exhausted") from None
        if isinstance(item, ProviderError):
            raise item
        step = item(request) if callable(item) else item
        self._n += 1
        calls = tuple(ToolCall(f"toolu_fake_{self._n}_{i}", name, dict(args)) for i, (name, args) in enumerate(step.calls))
        blocks = ((TextBlock(step.text),) if step.text else ()) + tuple(ToolUseBlock(c.id, c.name, c.input) for c in calls)
        return ProviderResult(calls, step.text, step.usage, step.stop_reason, f"req_fake_{self._n}", 0.0, blocks)
