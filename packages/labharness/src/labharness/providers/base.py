"""Provider-neutral request/response types and the one interface every adapter implements.

`Provider.step(request) -> ProviderResult` is the plan's `step(messages, tools) ->
ProviderResult{actions, summary, usage}`. Adapters convert these neutral types to the
provider's wire format; nothing above this layer knows a vendor.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


# ---- messages -----------------------------------------------------------------------
@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ImageBlock:
    png: bytes


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant"
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class SystemBlock:
    text: str
    cache: bool = False  # mark as a stable prefix the provider may cache


@dataclass(frozen=True)
class ProviderRequest:
    system: tuple[SystemBlock, ...]
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...]
    max_output_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        """Every token the provider reports, so the token cap is conservative."""
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_input_tokens + self.cache_read_input_tokens)


@dataclass(frozen=True)
class ProviderResult:
    actions: tuple[ToolCall, ...]
    summary: str                  # the model's own text for this step
    usage: Usage
    stop_reason: str
    request_id: str | None = None
    latency_s: float = 0.0
    raw_assistant: tuple[Block, ...] = field(default=())  # blocks to replay in the context


# ---- errors ---------------------------------------------------------------------------
class ProviderError(Exception):
    """Base. `code` is stable and safe to log/show; `str(exc)` never contains secrets."""

    code = "provider_error"
    retryable = False

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class KeyRejected(ProviderError):
    code = "key_rejected"


class RateLimited(ProviderError):
    code = "rate_limited"
    retryable = True

    def __init__(self, message: str = "", retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class Overloaded(ProviderError):
    code = "provider_overloaded"
    retryable = True


class ServerError(ProviderError):
    code = "provider_server_error"
    retryable = True


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    retryable = True


class BadRequest(ProviderError):
    code = "provider_bad_request"


class BadResponse(ProviderError):
    code = "provider_bad_response"


class Provider(Protocol):
    name: str
    model_id: str

    def step(self, request: ProviderRequest) -> ProviderResult: ...
