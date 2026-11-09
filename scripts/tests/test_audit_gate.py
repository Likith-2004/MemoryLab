import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_gate as g

TODAY = dt.date(2026, 11, 10)


def ignores(tmp_path: Path, body: str) -> tuple[set[str], list[str]]:
    f = tmp_path / "i.toml"
    f.write_text(body)
    return g.load_ignores(f, TODAY)


def test_an_ignore_needs_a_reason_and_a_near_expiry(tmp_path: Path) -> None:
    ok = '[[ignore]]\nid = "GHSA-1"\nreason = "only reachable from a dev server we never run"\nexpires = 2027-01-15\n'
    assert ignores(tmp_path, ok) == ({"GHSA-1"}, [])
    _, p = ignores(tmp_path, ok.replace("2027-01-15", "2026-11-01"))
    assert p and "expired" in p[0]
    _, p = ignores(tmp_path, ok.replace("2027-01-15", "2028-01-15"))
    assert p and "180 days" in p[0]
    _, p = ignores(tmp_path, ok.replace('reason = "only reachable from a dev server we never run"', 'reason = "no"'))
    assert p and "reason" in p[0]
    _, p = ignores(tmp_path, ok.replace("expires = 2027-01-15\n", ""))
    assert p and "expiry" in p[0]
    assert ignores(tmp_path, "")[1] == []


def osv(score: str | None, ids: list[str]) -> dict:  # type: ignore[type-arg]
    group = {"ids": ids, "aliases": ids}
    if score is not None:
        group["max_severity"] = score  # type: ignore[assignment]
    return {"results": [{"packages": [{"package": {"name": "x", "version": "1", "ecosystem": "npm"}, "groups": [group]}]}]}


def test_osv_fails_on_high_critical_and_unscored_but_not_on_medium() -> None:
    assert g.evaluate("osv", osv("6.9", ["GHSA-m"]), set()) == []
    assert g.evaluate("osv", osv("7.0", ["GHSA-h"]), set())
    assert g.evaluate("osv", osv("9.8", ["GHSA-c"]), set())
    assert g.evaluate("osv", osv(None, ["GHSA-u"]), set())                      # unscored: assume the worst
    assert g.evaluate("osv", osv("9.8", ["GHSA-c", "CVE-1"]), {"CVE-1"}) == []   # an ignore by any alias


def test_npm_and_pip() -> None:
    npm = {"vulnerabilities": {"a": {"severity": "high", "via": [{"url": "https://github.com/advisories/GHSA-aaaa"}]},
                               "b": {"severity": "moderate", "via": []}}}
    assert len(g.evaluate("npm", npm, set())) == 1 and g.evaluate("npm", npm, {"GHSA-aaaa"}) == []
    pip = {"dependencies": [{"name": "p", "version": "1", "vulns": [{"id": "PYSEC-1", "aliases": ["CVE-2"]}]}, {"name": "q", "version": "1", "vulns": []}]}
    assert len(g.evaluate("pip", pip, set())) == 1 and g.evaluate("pip", pip, {"CVE-2"}) == []
