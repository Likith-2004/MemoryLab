import base64
import logging
import urllib.parse

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from runlog.redaction import KEY_PATTERNS, REDACTED, Redactor, ScrubFilter, scan_bytes, scan_text

KEY = "sk-ant-api03-Zq8vN2xLm4Kp7RtWc1YbH6dFg3JsAe5UoIi9PlXzC0nVq"
ODD_KEY = "visitor-chosen-secret-value-12345"  # no known prefix: only exact matching can catch it


def test_exact_key_and_all_encodings_are_redacted() -> None:
    r = Redactor([ODD_KEY])
    forms = [ODD_KEY, base64.b64encode(ODD_KEY.encode()).decode(), base64.urlsafe_b64encode(ODD_KEY.encode()).decode(),
             urllib.parse.quote(ODD_KEY, safe=""), ODD_KEY.encode().hex(), ODD_KEY[::-1],
             base64.b64encode(ODD_KEY.encode()).decode().rstrip("="), f'"{ODD_KEY}"']
    for form in forms:
        out = r.text(f"before {form} after")
        assert ODD_KEY not in out and form[:12] not in out and REDACTED in out, form
    assert r.hits >= len(forms)


def decodes_to_contain(text: str, needle: bytes) -> bool:
    """Independent check: could any base64 reading of `text` (any alignment) contain the key?"""
    for piece in text.split(REDACTED):
        for offset in range(4):
            chunk = piece[offset:]
            chunk = chunk[: len(chunk) // 4 * 4]
            try:
                if needle in base64.b64decode(chunk, validate=False):
                    return True
            except ValueError:
                continue
    return False


@pytest.mark.parametrize("shift", [0, 1, 2])
def test_base64_of_the_key_at_any_alignment(shift: int) -> None:
    r = Redactor([ODD_KEY])
    blob = base64.b64encode(b"x" * shift + ODD_KEY.encode() + b"tail").decode()
    assert decodes_to_contain(blob, ODD_KEY.encode())          # the check can see the key before redaction
    out = r.text(blob)
    assert REDACTED in out and not decodes_to_contain(out, ODD_KEY.encode())


def test_known_key_patterns_are_scrubbed_without_knowing_the_key() -> None:
    r = Redactor()
    samples = {"anthropic_key": KEY, "openai_style_key": "sk-proj-" + "a1B2" * 12, "bearer_token": "Authorization: Bearer abcDEF123456789012345678",
               "aws_access_key": "AKIAIOSFODNN7EXAMPLE", "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
               "google_api_key": "AIza" + "x" * 35, "private_key_block": "-----BEGIN RSA PRIVATE KEY-----"}
    for name, sample in samples.items():
        assert name in scan_text(f"leak: {sample}"), name
        assert name in scan_bytes(f"leak: {sample}".encode())
        out = r.text(f"leak: {sample}")
        assert scan_text(out) == [] and REDACTED in out, name
    assert set(samples) == set(KEY_PATTERNS)


def test_clean_text_is_untouched_and_counts_no_hits() -> None:
    r = Redactor([ODD_KEY])
    for text in ("go right then press A", "sk-short", "Bearer short", "the key is hidden", ""):
        assert r.text(text) == text
    assert r.hits == 0


def test_obj_redacts_nested_values_and_keys_without_mutating_the_input() -> None:
    r = Redactor([ODD_KEY])
    data = {"a": [ODD_KEY, {"b": f"x {ODD_KEY} y"}], ODD_KEY: 1, "n": 5, "t": True, "none": None}
    out = r.obj(data)
    assert ODD_KEY not in str(out) and out["n"] == 5 and out["t"] is True and out["none"] is None
    assert ODD_KEY in str(data)  # input untouched


def test_redactor_never_reveals_the_key() -> None:
    r = Redactor([ODD_KEY])
    r.text(ODD_KEY)
    assert ODD_KEY not in repr(r) and ODD_KEY not in str(vars(r).get("hits")) and "hits=1" in repr(r)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=40), st.text(max_size=40), st.sampled_from(["plain", "b64", "url", "hex", "json"]))
def test_key_embedded_in_arbitrary_text_never_survives(prefix: str, suffix: str, form: str) -> None:
    r = Redactor([ODD_KEY])
    raw = ODD_KEY.encode()
    encoded = {"plain": ODD_KEY, "b64": base64.b64encode(raw).decode(), "url": urllib.parse.quote(ODD_KEY, safe=""),
               "hex": raw.hex(), "json": ODD_KEY}[form]
    out = r.text(prefix + encoded + suffix)
    assert ODD_KEY not in out and encoded not in out


def test_scrub_filter_cleans_log_records_and_args() -> None:
    f = ScrubFilter(Redactor([ODD_KEY]))
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed with %s and %s", (ODD_KEY, KEY), None)
    assert f.filter(record)
    rendered = record.getMessage()
    assert ODD_KEY not in rendered and KEY not in rendered and rendered.count(REDACTED) == 2
    plain = logging.LogRecord("x", logging.INFO, __file__, 1, f"msg {KEY}", None, None)
    f.filter(plain)
    assert KEY not in plain.getMessage()


def test_scrub_filter_leaves_numbers_booleans_and_none_alone_and_still_scrubs_strings() -> None:
    f = ScrubFilter(Redactor([ODD_KEY]))
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "reaped %d runs in %.1fs ok=%s none=%s key=%s",
                               (3, 1.5, True, None, ODD_KEY), None)
    assert f.filter(record)
    assert record.args[:4] == (3, 1.5, True, None)                  # type: ignore[index]
    rendered = record.getMessage()
    assert rendered.startswith("reaped 3 runs in 1.5s ok=True none=None") and ODD_KEY not in rendered


def test_scrub_filter_handles_the_mapping_form_and_non_string_objects() -> None:
    f = ScrubFilter(Redactor([ODD_KEY]))
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "n=%(n)d key=%(k)s err=%(e)s",
                               ({"n": 7, "k": ODD_KEY, "e": ValueError(ODD_KEY)},), None)
    assert f.filter(record)
    rendered = record.getMessage()
    assert rendered.startswith("n=7 ") and ODD_KEY not in rendered


@pytest.mark.parametrize("short", ["abcd1234", "k" * 9, "x1y2z3w4v5u"])
def test_short_keys_are_still_redacted_exactly(short: str) -> None:
    r = Redactor([short])
    assert short not in r.text(f"a {short} b") and r.hits == 1
