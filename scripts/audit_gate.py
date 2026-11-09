"""Fail the dependency audit on high or critical advisories, honouring security/audit-ignores.toml (each ignore needs a reason and an expiry).

    audit_gate.py osv  osv.json     osv-scanner --format json            (fails on CVSS >= 7.0 or an unscored advisory)
    audit_gate.py npm  audit.json   npm audit --json                     (fails on severity high or critical)
    audit_gate.py pip  audit.json   pip-audit --format json              (pip-audit reports no severity: any advisory fails)
"""

import datetime as dt
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

IGNORES = Path(__file__).resolve().parent.parent / "security" / "audit-ignores.toml"
MAX_DAYS = 180
HIGH = 7.0


def load_ignores(path: Path = IGNORES, today: dt.date | None = None) -> tuple[set[str], list[str]]:
    """(ids ignored, problems with the ignore file itself)."""
    today = today or dt.datetime.now(dt.UTC).date()
    ids: set[str] = set()
    problems: list[str] = []
    for i, entry in enumerate(tomllib.loads(path.read_text()).get("ignore", [])):
        ident, reason, expires = entry.get("id"), str(entry.get("reason", "")).strip(), entry.get("expires")
        if not ident:
            problems.append(f"ignore #{i + 1}: no id")
            continue
        if len(reason) < 15:
            problems.append(f"{ident}: give a real reason")
        if not isinstance(expires, dt.date):
            problems.append(f"{ident}: no expiry date")
        elif expires < today:
            problems.append(f"{ident}: the ignore expired on {expires}; fix the dependency or renew it with a new reason")
        elif (expires - today).days > MAX_DAYS:
            problems.append(f"{ident}: expires {expires}, more than {MAX_DAYS} days away")
        ids.add(str(ident))
    return ids, problems


def osv_findings(doc: dict[str, Any]) -> list[tuple[str, set[str], float | None, str]]:
    out = []
    for result in doc.get("results", []):
        for pkg in result.get("packages", []):
            p = pkg["package"]
            for group in pkg.get("groups", []):
                score = group.get("max_severity")
                out.append((f"{p['ecosystem']}:{p['name']}@{p['version']}", set(group.get("ids", [])) | set(group.get("aliases", [])),
                            float(score) if score not in (None, "") else None, ", ".join(group.get("ids", []))))
    return out


def npm_findings(doc: dict[str, Any]) -> list[tuple[str, set[str], float | None, str]]:
    scale = {"info": 0.0, "low": 3.0, "moderate": 5.0, "high": 8.0, "critical": 9.5}
    out = []
    for name, v in doc.get("vulnerabilities", {}).items():
        ids: set[str] = set()
        for via in v.get("via", []):
            if isinstance(via, dict):
                url = str(via.get("url", ""))
                ids |= {url.rsplit("/", 1)[-1]} if url else set()
        out.append((f"npm:{name}", ids, scale.get(v.get("severity", ""), None), ", ".join(sorted(ids)) or v.get("severity", "")))
    return out


def pip_findings(doc: dict[str, Any]) -> list[tuple[str, set[str], float | None, str]]:
    out = []
    for dep in doc.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            out.append((f"pip:{dep['name']}@{dep['version']}", {vuln["id"], *vuln.get("aliases", [])}, None, vuln["id"]))
    return out


def evaluate(kind: str, doc: dict[str, Any], ignored: set[str]) -> list[str]:
    findings = {"osv": osv_findings, "npm": npm_findings, "pip": pip_findings}[kind](doc)
    failing = []
    for where, ids, score, label in findings:
        if ids & ignored:
            continue
        if kind == "pip" or score is None or score >= HIGH:
            failing.append(f"{where}: {label} (severity {'unknown' if score is None else score})")
    return failing


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("osv", "npm", "pip"):
        print(__doc__)
        return 2
    ignored, problems = load_ignores()
    failing = evaluate(argv[1], json.loads(Path(argv[2]).read_text() or "{}"), ignored)
    for line in problems:
        print("ignore file:", line)
    for line in failing:
        print("vulnerable:", line)
    if not problems and not failing:
        print(f"{argv[1]}: no unignored high or critical advisories")
    return 1 if problems or failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
