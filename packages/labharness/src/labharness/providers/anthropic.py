"""Anthropic Messages API adapter (raw HTTPS via httpx).

Security properties (tested with a canary key):
- the key lives in a `SecretKey`, is read once per request to build one header, and never
  appears in reprs, exceptions, logs, or events;
- the host is fixed to api.anthropic.com over TLS: no base-URL override, no redirects, no proxy
  or CA settings from the environment;
- errors are mapped to stable codes; response bodies and request details are not echoed.

NOT VERIFIED AGAINST THE LIVE API in this repository's CI (no key available): request/response
shapes follow Anthropic's public Messages API documentation and are covered by recorded
fixtures and a mock transport only.
"""

import base64
import json
import random
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from .base import (
    BadRequest,
    BadResponse,
    ImageBlock,
    KeyRejected,
    Message,
    Overloaded,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeout,
    RateLimited,
    ServerError,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .registry import PROVIDER_HOSTS, check_model
from .secret import SecretKey

API_VERSION = "2023-06-01"
BASE_URL = f"https://{PROVIDER_HOSTS['anthropic']}"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RETRY_DELAY_S = 30.0
_SAFE_TYPE = re.compile(r"[a-z_]{1,40}")
_PROXY = re.compile(r"http://[A-Za-z0-9.\-]{1,253}:[0-9]{1,5}")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        key: SecretKey,
        model_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        cache_system_prompt: bool = True,
        proxy: str | None = None,
    ) -> None:
        if not isinstance(key, SecretKey):
            raise TypeError("key must be a SecretKey")
        check_model("anthropic", model_id)
        if not 0 <= max_retries <= 6:
            raise ValueError("max_retries must be 0..6")
        self.model_id = model_id
        self._key = key
        self._max_retries = max_retries
        self._sleep, self._monotonic, self._jitter = sleep, monotonic, jitter
        self._cache_system = cache_system_prompt
        if proxy is not None and not _PROXY.fullmatch(proxy):
            raise ValueError("proxy must look like http://host:port (no credentials, no path)")
        self._client = httpx.Client(
            base_url=BASE_URL, transport=transport, follow_redirects=False, trust_env=False, proxy=proxy,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    def __repr__(self) -> str:
        return f"AnthropicProvider(model_id={self.model_id!r})"

    def close(self) -> None:
        self._client.close()

    # ---- public API ---------------------------------------------------------------
    def validate_key(self, timeout_s: float = 10.0) -> None:
        """One cheap, token-free call (list models). Raises KeyRejected for a bad key; the
        caller must show only the generic `key_rejected` code."""
        status, _, _ = self._send("GET", "/v1/models?limit=1", None, timeout_s)
        if status != 200:
            raise self._error_for(status, b"", None)

    def step(self, request: ProviderRequest) -> ProviderResult:
        body = self._build_body(request)
        started = self._monotonic()
        status, raw, headers = self._send("POST", "/v1/messages", body, request.timeout_s)
        if status != 200:
            raise self._error_for(status, raw, headers)
        return self._parse(raw, headers.get("request-id"), self._monotonic() - started)

    # ---- request ----------------------------------------------------------------------
    def _build_body(self, request: ProviderRequest) -> bytes:
        if not 1 <= request.max_output_tokens <= 64000:
            raise BadRequest("max_output_tokens out of range")
        system = [
            {"type": "text", "text": s.text, **({"cache_control": {"type": "ephemeral"}} if s.cache and self._cache_system else {})}
            for s in request.system
        ]
        body: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": request.max_output_tokens,
            "system": system,
            "messages": [self._message(m) for m in request.messages],
        }
        if request.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                             for t in request.tools]
            body["tool_choice"] = {"type": "any"}  # every step must act; text-only steps waste budget
        return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode()

    @staticmethod
    def _message(message: Message) -> dict[str, Any]:
        if message.role not in ("user", "assistant"):
            raise BadRequest("unknown message role")
        content: list[dict[str, Any]] = []
        seen_non_result = False
        for block in message.blocks:
            if isinstance(block, TextBlock):
                content.append({"type": "text", "text": block.text})
                seen_non_result = True
            elif isinstance(block, ImageBlock):
                data = base64.b64encode(block.png).decode()
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
                seen_non_result = True
            elif isinstance(block, ToolUseBlock):
                if message.role != "assistant":
                    raise BadRequest("tool_use only allowed in assistant messages")
                content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif isinstance(block, ToolResultBlock):
                if message.role != "user" or seen_non_result:
                    raise BadRequest("tool_result must come first in a user message")
                item: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
                if block.is_error:
                    item["is_error"] = True
                content.append(item)
        if not content:
            raise BadRequest("empty message")
        return {"role": message.role, "content": content}

    # ---- transport with bounded retries -----------------------------------------------------
    def _send(self, method: str, path: str, body: bytes | None, total_timeout_s: float) -> tuple[int, bytes, httpx.Headers]:
        deadline = self._monotonic() + total_timeout_s
        last: ProviderError | None = None
        for attempt in range(self._max_retries + 1):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            result, failure = self._attempt(method, path, body, remaining)
            if failure is None and result is not None:
                status, raw, headers = result
                if status == 200 or not self._error_for(status, raw, headers).retryable:
                    return result
                failure = self._error_for(status, raw, headers)
            assert failure is not None
            last = failure
            if not failure.retryable or attempt == self._max_retries:
                break
            delay = self._delay(attempt, failure)
            if self._monotonic() + delay >= deadline:
                break
            self._sleep(delay)
        raise last or ProviderTimeout("call budget exhausted before a response")

    def _delay(self, attempt: int, failure: ProviderError) -> float:
        hinted = failure.retry_after_s if isinstance(failure, RateLimited) else None
        base = hinted if hinted is not None else min(MAX_RETRY_DELAY_S, 1.0 * 2**attempt)
        return min(MAX_RETRY_DELAY_S, base) * (1 + 0.25 * self._jitter())

    def _attempt(self, method: str, path: str, body: bytes | None, timeout_s: float,
                 ) -> tuple[tuple[int, bytes, httpx.Headers] | None, ProviderError | None]:
        headers = {"x-api-key": self._key.reveal(), "anthropic-version": API_VERSION,
                   "content-type": "application/json", "user-agent": "memory-lab/0.2"}
        result: tuple[int, bytes, httpx.Headers] | None = None
        failure: ProviderError | None = None
        try:
            request = self._client.build_request(method, path, content=body, headers=headers,
                                                 timeout=httpx.Timeout(timeout_s))
            response = self._client.send(request, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        failure = BadResponse("response too large")
                        break
                    chunks.append(chunk)
                else:
                    result = (response.status_code, b"".join(chunks), response.headers)
            finally:
                response.close()
        except httpx.TimeoutException:
            failure = ProviderTimeout("provider call timed out")
        except httpx.HTTPError:
            failure = ServerError("could not reach the provider")
        # Raised outside the handler so no exception (which holds the request and its headers)
        # is chained onto ours.
        return result, failure

    # ---- responses ------------------------------------------------------------------------------
    @staticmethod
    def _error_for(status: int, raw: bytes, headers: httpx.Headers | None) -> ProviderError:
        kind = ""
        try:
            doc = json.loads(raw) if raw else {}
            candidate = doc.get("error", {}).get("type", "") if isinstance(doc, dict) else ""
            kind = candidate if isinstance(candidate, str) and _SAFE_TYPE.fullmatch(candidate) else ""
        except (ValueError, AttributeError):
            kind = ""
        suffix = f" ({kind})" if kind else ""
        if status in (401, 403):
            return KeyRejected("the provider rejected the key")
        if status == 429:
            retry_after = None
            if headers is not None:
                try:
                    retry_after = max(0.0, min(MAX_RETRY_DELAY_S, float(headers.get("retry-after", ""))))
                except ValueError:
                    retry_after = None
            return RateLimited(f"rate limited{suffix}", retry_after)
        if status == 529:
            return Overloaded(f"provider overloaded{suffix}")
        if 500 <= status < 600:
            return ServerError(f"provider error {status}{suffix}")
        if 300 <= status < 400:
            return BadResponse("unexpected redirect")
        return BadRequest(f"provider rejected the request ({status}){suffix}")

    @staticmethod
    def _parse(raw: bytes, request_id: str | None, latency_s: float) -> ProviderResult:
        doc: Any = None
        valid = True
        try:
            doc = json.loads(raw)
        except ValueError:
            valid = False
        if not valid:  # raised outside the handler: nothing is chained onto our error
            raise BadResponse("response was not valid JSON")
        if not isinstance(doc, dict) or not isinstance(doc.get("content"), list):
            raise BadResponse("response has no content list")
        usage_doc = doc.get("usage")
        if not isinstance(usage_doc, dict):
            raise BadResponse("response has no usage")

        def count(name: str, required: bool = False) -> int:
            value = usage_doc.get(name, None if required else 0)
            if value is None and not required:
                return 0
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BadResponse(f"usage.{name} is not a non-negative integer")
            return value

        usage = Usage(count("input_tokens", True), count("output_tokens", True),
                      count("cache_creation_input_tokens"), count("cache_read_input_tokens"))
        texts: list[str] = []
        calls: list[ToolCall] = []
        blocks: list[TextBlock | ToolUseBlock] = []
        for item in doc["content"]:
            if not isinstance(item, dict):
                raise BadResponse("content block is not an object")
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
                blocks.append(TextBlock(item["text"]))
            elif item.get("type") == "tool_use":
                if not (isinstance(item.get("id"), str) and isinstance(item.get("name"), str)
                        and isinstance(item.get("input"), dict)):
                    raise BadResponse("malformed tool_use block")
                calls.append(ToolCall(item["id"], item["name"], item["input"]))
                blocks.append(ToolUseBlock(item["id"], item["name"], item["input"]))
        stop = doc.get("stop_reason")
        return ProviderResult(tuple(calls), "\n".join(texts), usage, stop if isinstance(stop, str) else "unknown",
                              request_id, latency_s, tuple(blocks))
