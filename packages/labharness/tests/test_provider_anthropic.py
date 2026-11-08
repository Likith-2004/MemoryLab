import base64
import copy
import json
import logging
import pickle
import socket
import traceback

import httpx
import pytest

from labharness.providers import (
    BadRequest,
    BadResponse,
    ImageBlock,
    KeyRejected,
    Message,
    Overloaded,
    ProviderError,
    ProviderRequest,
    ProviderTimeout,
    RateLimited,
    SecretKey,
    ServerError,
    SystemBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from labharness.providers.anthropic import MAX_RESPONSE_BYTES, AnthropicProvider

CANARY = "sk-ant-api03-CANARYCANARYCANARYCANARYCANARY0123456789-canary"
OK_BODY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "Going right."},
                {"type": "tool_use", "id": "toolu_1", "name": "press", "input": {"buttons": ["Right"], "hold_frames": 2}},
                {"type": "thinking", "thinking": "ignored", "signature": "x"}],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 120, "output_tokens": 30, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7},
}
TOOLS = (ToolSchema("press", "press buttons", {"type": "object", "properties": {}, "additionalProperties": False}),)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_a: object, **_k: object) -> None:
        raise AssertionError("real network access attempted")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def req(**kw: object) -> ProviderRequest:
    base = {"system": (SystemBlock("You play a game.", cache=True),),
            "messages": (Message("user", (TextBlock("obs"),)),), "tools": TOOLS,
            "max_output_tokens": 512, "timeout_s": 30.0}
    return ProviderRequest(**{**base, **kw})  # type: ignore[arg-type]


class Rec:
    """Mock transport handler: serves queued responses/exceptions, records requests."""

    def __init__(self, *responses: object) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, httpx.Response)
        return item


def resp(status: int = 200, body: object = OK_BODY, headers: dict[str, str] | None = None) -> httpx.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, content=content, headers={"request-id": "req_abc", **(headers or {})})


def provider(rec: Rec, **kw: object) -> tuple[AnthropicProvider, list[float]]:
    sleeps: list[float] = []
    p = AnthropicProvider(SecretKey(CANARY), "claude-sonnet-5-5", transport=httpx.MockTransport(rec),
                          sleep=sleeps.append, jitter=lambda: 0.0, **kw)  # type: ignore[arg-type]
    return p, sleeps


# ---- request shape ---------------------------------------------------------------------------
def test_request_shape() -> None:
    rec = Rec(resp())
    p, _ = provider(rec)
    png = b"\x89PNG\r\n\x1a\nfake"
    messages = (
        Message("user", (TextBlock("first obs"),)),
        Message("assistant", (TextBlock("thinking"), ToolUseBlock("toolu_0", "press", {"buttons": ["A"], "hold_frames": 1}))),
        Message("user", (ToolResultBlock("toolu_0", "ok"), ToolResultBlock("toolu_x", "bad", True), TextBlock("obs 2"), ImageBlock(png))),
    )
    p.step(req(messages=messages))
    (r,) = rec.requests
    assert (r.method, r.url.host, r.url.path, r.url.scheme) == ("POST", "api.anthropic.com", "/v1/messages", "https")
    assert r.headers["x-api-key"] == CANARY and r.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(r.content)
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 512
    assert body["system"] == [{"type": "text", "text": "You play a game.", "cache_control": {"type": "ephemeral"}}]
    assert body["tools"][0]["name"] == "press" and body["tool_choice"] == {"type": "any"}
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][1] == {"type": "tool_use", "id": "toolu_0", "name": "press", "input": {"buttons": ["A"], "hold_frames": 1}}
    last = body["messages"][2]["content"]
    assert [c["type"] for c in last] == ["tool_result", "tool_result", "text", "image"]
    assert "is_error" not in last[0] and last[1]["is_error"] is True
    assert base64.b64decode(last[3]["source"]["data"]) == png and last[3]["source"]["media_type"] == "image/png"
    assert "temperature" not in body  # not sent unless configured: unverified for new models


def test_no_tools_means_no_tool_fields_and_cache_can_be_disabled() -> None:
    rec = Rec(resp())
    p, _ = provider(rec, cache_system_prompt=False)
    p.step(req(tools=()))
    body = json.loads(rec.requests[0].content)
    assert "tools" not in body and "tool_choice" not in body and "cache_control" not in body["system"][0]


@pytest.mark.parametrize(
    "message",
    [Message("user", (TextBlock("x"), ToolResultBlock("t", "r"))),                 # result after text
     Message("assistant", (ToolResultBlock("t", "r"),)),                           # result from assistant
     Message("user", (ToolUseBlock("t", "press", {}),)),                           # tool_use from user
     Message("user", ()), Message("system", (TextBlock("x"),))],
)
def test_malformed_message_structure_is_refused_before_sending(message: Message) -> None:
    rec = Rec(resp())
    p, _ = provider(rec)
    with pytest.raises(BadRequest):
        p.step(req(messages=(message,)))
    assert rec.requests == []


@pytest.mark.parametrize("n", [0, -1, 64001])
def test_output_token_bounds(n: int) -> None:
    rec = Rec(resp())
    with pytest.raises(BadRequest):
        provider(rec)[0].step(req(max_output_tokens=n))
    assert rec.requests == []


# ---- response parsing ---------------------------------------------------------------------------
def test_response_parsing() -> None:
    p, _ = provider(Rec(resp()))
    out = p.step(req())
    assert out.summary == "Going right." and out.stop_reason == "tool_use" and out.request_id == "req_abc"
    assert [(c.id, c.name, c.input) for c in out.actions] == [("toolu_1", "press", {"buttons": ["Right"], "hold_frames": 2})]
    assert (out.usage.input_tokens, out.usage.output_tokens, out.usage.total) == (120, 30, 162)
    assert len(out.raw_assistant) == 2  # unknown (thinking) blocks are not replayed


def test_text_only_and_missing_optional_usage() -> None:
    body = {"content": [{"type": "text", "text": "hmm"}], "usage": {"input_tokens": 3, "output_tokens": 1}, "stop_reason": "end_turn"}
    out = provider(Rec(resp(body=body)))[0].step(req())
    assert out.actions == () and out.usage.total == 4


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b"null", {"usage": {"input_tokens": 1, "output_tokens": 1}},
     {"content": {}, "usage": {"input_tokens": 1, "output_tokens": 1}}, {"content": []},
     {"content": [], "usage": {"input_tokens": 1}}, {"content": [], "usage": {"input_tokens": -1, "output_tokens": 1}},
     {"content": [], "usage": {"input_tokens": True, "output_tokens": 1}},
     {"content": [], "usage": {"input_tokens": "5", "output_tokens": 1}},
     {"content": ["x"], "usage": {"input_tokens": 1, "output_tokens": 1}},
     {"content": [{"type": "tool_use", "id": "a", "name": "press", "input": []}], "usage": {"input_tokens": 1, "output_tokens": 1}},
     {"content": [{"type": "tool_use", "id": 5, "name": "press", "input": {}}], "usage": {"input_tokens": 1, "output_tokens": 1}}],
)
def test_malformed_responses_are_bad_response(body: object) -> None:
    with pytest.raises(BadResponse):
        provider(Rec(resp(body=body)))[0].step(req())


# ---- error mapping ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "cls", "code", "retryable"),
    [(400, BadRequest, "provider_bad_request", False), (401, KeyRejected, "key_rejected", False),
     (403, KeyRejected, "key_rejected", False), (404, BadRequest, "provider_bad_request", False),
     (413, BadRequest, "provider_bad_request", False), (422, BadRequest, "provider_bad_request", False),
     (429, RateLimited, "rate_limited", True), (500, ServerError, "provider_server_error", True),
     (502, ServerError, "provider_server_error", True), (503, ServerError, "provider_server_error", True),
     (529, Overloaded, "provider_overloaded", True), (302, BadResponse, "provider_bad_response", False)],
)
def test_status_mapping(status: int, cls: type[ProviderError], code: str, retryable: bool) -> None:
    p, sleeps = provider(Rec(resp(status, {"error": {"type": "invalid_request_error", "message": "secret prompt text"}})), )
    with pytest.raises(cls) as info:
        p.step(req())
    assert info.value.code == code and info.value.retryable is retryable
    assert "secret prompt text" not in str(info.value)           # bodies are never echoed
    assert (len(sleeps) == 3) is retryable                       # retried 3 times iff retryable


def test_error_type_is_echoed_only_if_it_looks_safe() -> None:
    with pytest.raises(BadRequest, match=r"\(invalid_request_error\)"):
        provider(Rec(resp(400, {"error": {"type": "invalid_request_error", "message": "m"}})))[0].step(req())
    with pytest.raises(BadRequest) as info:
        provider(Rec(resp(400, {"error": {"type": "Ignore previous <script>", "message": "m"}})))[0].step(req())
    assert "script" not in str(info.value)


# ---- retries and backoff ----------------------------------------------------------------------------------
def test_retry_after_is_honoured_then_success() -> None:
    rec = Rec(resp(429, {"error": {"type": "rate_limit_error"}}, {"retry-after": "2"}), resp())
    p, sleeps = provider(rec)
    assert p.step(req()).summary == "Going right."
    assert sleeps == [2.0] and len(rec.requests) == 2


def test_retry_after_is_capped_and_garbage_ignored() -> None:
    p, sleeps = provider(Rec(resp(429, {}, {"retry-after": "9999"}), resp(429, {}, {"retry-after": "soon"}), resp()))
    p.step(req(timeout_s=300.0))
    assert sleeps == [30.0, 2.0]  # capped at 30; garbage falls back to exponential (attempt 1 -> 2s)


def test_exponential_backoff_with_jitter_bounds() -> None:
    sleeps: list[float] = []
    rec = Rec(resp(529), resp(529), resp(529), resp())
    p = AnthropicProvider(SecretKey(CANARY), "claude-sonnet-5-5", transport=httpx.MockTransport(rec),
                          sleep=sleeps.append, jitter=lambda: 1.0)
    p.step(req())
    assert sleeps == [1.25, 2.5, 5.0]


def test_retries_exhausted_raises_last_error_and_makes_exactly_four_attempts() -> None:
    rec = Rec(resp(529))
    p, sleeps = provider(rec)
    with pytest.raises(Overloaded):
        p.step(req())
    assert len(rec.requests) == 4 and len(sleeps) == 3
    rec0 = Rec(resp(500))
    with pytest.raises(ServerError):
        provider(rec0, max_retries=0)[0].step(req())
    assert len(rec0.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_errors_are_tried_once(status: int) -> None:
    rec = Rec(resp(status))
    with pytest.raises(ProviderError):
        provider(rec)[0].step(req())
    assert len(rec.requests) == 1


def test_timeouts_and_connection_errors_are_mapped_and_retried() -> None:
    rec = Rec(httpx.ReadTimeout("t"), httpx.ConnectError("c"), resp())
    p, sleeps = provider(rec)
    assert p.step(req()).stop_reason == "tool_use" and len(sleeps) == 2
    with pytest.raises(ProviderTimeout):
        provider(Rec(httpx.ConnectTimeout("t")), max_retries=0)[0].step(req())
    with pytest.raises(ServerError):
        provider(Rec(httpx.ConnectError("c")), max_retries=0)[0].step(req())


def test_total_time_budget_stops_retrying() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    rec = Rec(resp(529))
    p = AnthropicProvider(SecretKey(CANARY), "claude-sonnet-5-5", transport=httpx.MockTransport(rec), sleep=sleep,
                          monotonic=lambda: now[0], jitter=lambda: 0.0)
    with pytest.raises(Overloaded):
        p.step(req(timeout_s=2.5))      # 1s sleep fits, the following 2s sleep would pass the deadline
    assert sleeps == [1.0] and len(rec.requests) == 2


def test_redirects_are_never_followed() -> None:
    rec = Rec(resp(302, b"", {"location": "https://evil.example/steal"}))
    with pytest.raises(BadResponse):
        provider(rec)[0].step(req())
    assert len(rec.requests) == 1 and rec.requests[0].url.host == "api.anthropic.com"


def test_oversized_response_is_refused() -> None:
    big = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(BadResponse, match="too large"):
        provider(Rec(big))[0].step(req())


def test_client_ignores_environment_proxies_and_ca_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/evil.pem")
    p, _ = provider(Rec(resp()))
    assert p._client.trust_env is False and p._client.follow_redirects is False
    assert str(p._client.base_url).startswith("https://api.anthropic.com")


# ---- construction and key validation -----------------------------------------------------------------------
def test_constructor_guards() -> None:
    with pytest.raises(TypeError):
        AnthropicProvider(CANARY, "claude-sonnet-5-5")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="allowlisted"):
        AnthropicProvider(SecretKey(CANARY), "gpt-4o")
    with pytest.raises(ValueError):
        AnthropicProvider(SecretKey(CANARY), "claude-sonnet-5-5", max_retries=7)


def test_validate_key_uses_a_token_free_call_and_is_generic_on_failure() -> None:
    rec = Rec(resp(200, {"data": []}))
    provider(rec)[0].validate_key()
    assert (rec.requests[0].method, rec.requests[0].url.path) == ("GET", "/v1/models") and rec.requests[0].content == b""
    for status in (401, 403):
        with pytest.raises(KeyRejected) as info:
            provider(Rec(resp(status, {"error": {"type": "authentication_error", "message": "invalid x-api-key sk-..."}})))[0].validate_key()
        assert str(info.value) == "the provider rejected the key"
    with pytest.raises(ServerError):
        provider(Rec(resp(500)), max_retries=1)[0].validate_key()


# ---- the key never leaks -----------------------------------------------------------------------------------
def test_secret_key_cannot_be_printed_pickled_copied_or_changed() -> None:
    k = SecretKey(CANARY)
    for text in (repr(k), str(k), f"{k}", f"{k!r}", "%s" % k, f"{k}", json.dumps({"k": repr(k)})):  # noqa: UP031
        assert CANARY not in text and "****" in text
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(k)
    with pytest.raises(AttributeError):
        k.value = "x"  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        k._value = "x"
    assert k.reveal() == CANARY and k != SecretKey(CANARY)
    for bad in ("", "short", "has space in it 12345", "nl\nnl12345678", "é" * 20, "x" * 513, 5, None):
        with pytest.raises(ValueError):
            SecretKey(bad)  # type: ignore[arg-type]


def test_canary_key_is_found_nowhere_after_every_kind_of_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "labharness", ""):
        logging.getLogger(name).setLevel(logging.DEBUG)
    outcomes = [resp(401), resp(400, {"error": {"type": "invalid_request_error", "message": f"bad {CANARY}"}}),
                resp(500), resp(529), resp(302), resp(body=b"junk"), httpx.ReadTimeout("t"), httpx.ConnectError("c")]
    seen: list[str] = []
    for item in outcomes:
        p, _ = provider(Rec(item), max_retries=1)
        seen += [repr(p), str(vars(p)), repr(p._client)]
        try:
            p.step(req())
        except ProviderError as exc:
            assert exc.__cause__ is None and exc.__context__ is None  # nothing chained that could hold the request
            seen += ["".join(traceback.format_exception(exc)), repr(exc), str(exc), repr(exc.args)]
    p, _ = provider(Rec(resp()))
    seen += [repr(p.step(req())), caplog.text]
    assert not [s for s in seen if CANARY in s or CANARY[:20] in s]


@pytest.mark.parametrize("proxy", ["http://egress:3128", "http://ml-egress.internal:3128", "http://10.0.0.5:8080"])
def test_proxy_option_accepts_plain_host_port(proxy: str) -> None:
    p = AnthropicProvider(SecretKey(CANARY), "claude-sonnet-5-5", proxy=proxy)
    assert p._client.trust_env is False
    p.close()


@pytest.mark.parametrize("proxy", ["https://egress:3128", "http://user:pw@egress:3128", "http://egress:3128/path", "egress:3128",
                                   "http://egress", "http://eg ress:3128", "socks5://egress:1080", ""])
def test_proxy_option_rejects_credentials_paths_and_other_schemes(proxy: str) -> None:
    with pytest.raises(ValueError, match="proxy"):
        AnthropicProvider(SecretKey(CANARY), "claude-sonnet-5-5", proxy=proxy)
