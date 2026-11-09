"""The daemon's JSON decoder is case-insensitive and lets a later key override an earlier one, so a body that is valid to a case-sensitive
reader can mean something else to the daemon. The filter must refuse ambiguity, allow only exact-case known fields at every level, and
forward a document it built itself."""

import copy
import json
from typing import Any

import pytest

from dockerproxy.policy import parse_create, route
from payloads import GOOD, NAME, POLICY


def refused(doc_text: str) -> list[str]:
    return parse_create(doc_text.encode(), NAME, POLICY)[0]


def with_extra(**extra: Any) -> str:
    doc = copy.deepcopy(GOOD)
    doc.update(extra)
    return json.dumps(doc)


# the reviewer's five payloads, each of which the daemon obeys and the first version of the filter let through
@pytest.mark.parametrize("extra", [
    {"image": "memorylab-egress:dev"},
    {"user": "0"},
    {"cmd": ["sh", "-c", "id"]},
    {"hostconfig": {"Binds": ["/tmp/x:/host:ro"]}},
    {"hostConfig": {"Privileged": True, "CapAdd": ["SYS_ADMIN"]}},
    {"hostConfig": {"Privileged": True, "Binds": ["/:/host"]}},
])
def test_a_second_spelling_of_a_field_is_refused(extra: dict[str, Any]) -> None:
    assert refused(with_extra(**extra))


def test_an_unknown_top_level_key_is_refused_in_any_case() -> None:
    for key in ("Privileged", "privileged", "Devices", "Mounts", "ExposedPorts", "Healthcheck", "NetworkDisabled", "Foo"):
        assert refused(with_extra(**{key: True})), key


def walk(node: Any, path: tuple[str, ...] = ()) -> Any:
    """Every (path, key) of every dict in the document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key
            yield from walk(value, (*path, key))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk(item, (*path, str(i)))


def at(doc: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc


KEYS = list(walk(GOOD))


def test_the_walk_covers_every_nesting_level() -> None:
    assert len(KEYS) > 90 and any(len(p) >= 3 for p, _ in KEYS)                # HostConfig.Ulimits[0].Name and the like


@pytest.mark.parametrize("path,key", KEYS, ids=[".".join((*p, k)) for p, k in KEYS])
def test_a_case_variant_of_any_key_at_any_level_is_refused(path: tuple[str, ...], key: str) -> None:
    for variant in {key.lower(), key.upper(), key.swapcase(), key[:1].lower() + key[1:]} - {key}:
        doc = copy.deepcopy(GOOD)
        at(doc, path)[variant] = {"Privileged": True, "Binds": ["/:/host"]}
        assert refused(json.dumps(doc)), (path, key, variant)


@pytest.mark.parametrize("path", sorted({p for p, _ in KEYS if not p or isinstance(at(GOOD, p), dict)}), ids=lambda p: ".".join(p) or "<top>")
def test_a_new_key_in_any_object_is_refused(path: tuple[str, ...]) -> None:
    doc = copy.deepcopy(GOOD)
    at(doc, path)["ZzNewKey"] = "x"
    assert refused(json.dumps(doc)), path


class Pairs(list):  # type: ignore[type-arg]
    """An object as its list of (key, value) pairs, so a test can write a duplicate key."""


def dumps(node: Any, dup: tuple[str, ...] | None, here: tuple[str, ...] = ()) -> str:
    if isinstance(node, dict):
        items = [(k, v) for k, v in node.items()]
        if dup is not None and here == dup[:-1]:
            items += [(dup[-1], node[dup[-1]])]                                  # the same key again, same value
        return "{" + ",".join(json.dumps(k) + ":" + dumps(v, dup, (*here, k)) for k, v in items) + "}"
    if isinstance(node, list):
        return "[" + ",".join(dumps(v, dup, (*here, str(i))) for i, v in enumerate(node)) + "]"
    return json.dumps(node)


def test_a_duplicate_key_at_every_level_is_refused() -> None:
    assert refused(dumps(GOOD, None)) == []                                      # the writer itself is sound
    for path, key in KEYS:
        assert refused(dumps(GOOD, (*path, key))), (path, key)


def test_a_duplicate_with_a_different_value_is_refused_too() -> None:
    text = json.dumps(GOOD)[:-1] + ',"User":"0"}'
    assert refused(text)
    text = json.dumps(GOOD, separators=(",", ":")).replace('"Privileged":false', '"Privileged":false,"Privileged":true')
    assert text != json.dumps(GOOD, separators=(",", ":"))
    assert refused(text)


def test_non_json_values_are_refused() -> None:
    for text in ("NaN", '{"HostConfig":NaN}', "[" * 5000, '{"a":' * 3000):
        assert refused(text)


# ---- what is forwarded is built by us ----------------------------------------------------------------------------------------------
def test_the_forwarded_document_is_the_canonical_rebuild_whatever_the_client_wrote() -> None:
    reasons, canonical = parse_create(json.dumps(GOOD).encode(), NAME, POLICY)
    assert reasons == [] and json.loads(canonical) == GOOD
    shuffled = json.dumps(dict(reversed(list(GOOD.items()))), indent=3, ensure_ascii=False)
    assert parse_create(shuffled.encode(), NAME, POLICY)[1] == canonical        # key order and whitespace do not survive
    assert b" " not in canonical.replace(b"no-new-privileges", b"").replace(b"rw,noexec,nosuid,nodev,size=64m", b"")
    assert parse_create(b"{}", NAME, POLICY)[1] == b""                          # nothing is forwarded for a refusal


# ---- bodies and versions -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("target", ["/v1.43/containers/x/json", "/v1.61/containers/x/json", "/v1.5/containers/x/json", "/v2.0/containers/x/json",
                                    "/v1.56x/containers/x/json", "/containers/x/json", "/version", "/V1.56/containers/x/json"])
def test_only_the_supported_api_versions_are_allowed_and_only_the_ping_may_go_without(target: str) -> None:
    assert not route("GET", target, POLICY).allow


@pytest.mark.parametrize("target", ["/v1.44/containers/x/json", "/v1.56/containers/x/json", "/v1.60/containers/x/json"])
def test_the_supported_versions_pass(target: str) -> None:
    assert route("GET", target, POLICY).allow
