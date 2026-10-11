"""External audit 2026-10-11, SEC-02: an advisory nobody has decided about
cannot reach a release because no requirement file changed.

tools/audit_gate.py decides; the workflow calls it as blocking on every push
to main (the run the release workflow requires green).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import audit_gate  # noqa: E402

TODAY = date(2026, 10, 11)


def _report(*vulns: tuple[str, str, str, list[str]]) -> dict:
    deps: dict[str, dict] = {}
    for package, version, vid, aliases in vulns:
        dep = deps.setdefault(package, {"name": package, "version": version, "vulns": []})
        dep["vulns"].append({"id": vid, "aliases": aliases, "fix_versions": ["9.9"]})
    deps.setdefault("clean", {"name": "clean", "version": "1.0", "vulns": []})
    return {"dependencies": list(deps.values())}


def _exceptions(*entries: str) -> list:
    return audit_gate.read_exceptions("\n".join(entries))


def _entry(vid: str, package: str, expires: str = "2026-12-31", **extra) -> str:
    fields = {"id": vid, "package": package, "owner": "@maintainer",
              "reason": "not reachable", **extra}
    body = "\n".join(f'{k} = "{v}"' for k, v in fields.items())
    return f"[[exception]]\n{body}\nexpires = {expires}\n"


def test_a_new_advisory_blocks_a_blocking_build():
    findings = audit_gate.read_findings(_report(("pillow", "10.0", "GHSA-AAAA", ["CVE-1"])))
    blocked, text = audit_gate.decide(findings, [], blocking=True, today=TODAY)
    assert blocked and "GHSA-AAAA" in text and "not accepted" in text


def test_the_same_advisory_is_reported_but_lets_an_unrelated_pull_request_through():
    findings = audit_gate.read_findings(_report(("pillow", "10.0", "GHSA-AAAA", [])))
    blocked, text = audit_gate.decide(findings, [], blocking=False, today=TODAY)
    assert not blocked and "GHSA-AAAA" in text and "not blocking" in text


def test_an_advisory_listed_twice_is_one_finding():
    report = _report(("requests", "2.19.0", "PYSEC-2018-28", []), ("requests", "2.19.0", "PYSEC-2018-28", []))
    assert len(audit_gate.read_findings(report)) == 1


def test_an_accepted_advisory_passes_and_stays_visible_with_its_owner():
    findings = audit_gate.read_findings(_report(("pillow", "10.0", "GHSA-AAAA", ["CVE-1"])))
    accepted = _exceptions(_entry("CVE-1", "Pillow"))     # by alias, any case
    blocked, text = audit_gate.decide(findings, accepted, blocking=True, today=TODAY)
    assert not blocked
    assert "Accepted, still present" in text and "@maintainer" in text and "2026-12-31" in text


def test_an_expired_acceptance_blocks_again():
    findings = audit_gate.read_findings(_report(("pillow", "10.0", "GHSA-AAAA", [])))
    accepted = _exceptions(_entry("GHSA-AAAA", "pillow", expires="2026-10-10"))
    blocked, text = audit_gate.decide(findings, accepted, blocking=True, today=TODAY)
    assert blocked and "expired 2026-10-10" in text


def test_an_acceptance_for_another_package_does_not_cover_this_one():
    findings = audit_gate.read_findings(_report(("pillow", "10.0", "GHSA-AAAA", [])))
    accepted = _exceptions(_entry("GHSA-AAAA", "numpy"))
    blocked, text = audit_gate.decide(findings, accepted, blocking=True, today=TODAY)
    assert blocked and "no longer found" in text


@pytest.mark.parametrize("bad", [
    _entry("GHSA-AAAA", "pillow", owner=""),
    '[[exception]]\nid = "GHSA-AAAA"\npackage = "pillow"\nowner = "@m"\nreason = "r"\nexpires = "soon"\n',
    '[[exception]]\nid = "GHSA-AAAA"\npackage = "pillow"\nowner = "@m"\nexpires = 2026-12-31\n',
])
def test_an_acceptance_without_an_owner_reason_or_real_date_is_refused(bad):
    with pytest.raises(ValueError):
        audit_gate.read_exceptions(bad)


def test_the_command_exits_by_the_decision(tmp_path, capsys):
    report = tmp_path / "audit.json"
    report.write_text(json.dumps(_report(("pillow", "10.0", "GHSA-AAAA", []))))
    none = tmp_path / "none.toml"
    assert audit_gate.main([str(report), str(none), "--blocking", "--today", "2026-10-11"]) == 1
    assert audit_gate.main([str(report), str(none), "--today", "2026-10-11"]) == 0
    report.write_text("not json")
    assert audit_gate.main([str(report), str(none), "--blocking"]) == 2


def test_the_shipped_exceptions_file_reads():
    audit_gate.read_exceptions((ROOT / "requirements" / "audit-exceptions.toml").read_text())


def test_the_workflow_blocks_every_push_and_never_ignores_the_audit_step():
    flow = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text())
    steps = next(job["steps"] for job in flow["jobs"].values()
                 if any("audit_gate.py" in str(s.get("run", "")) for s in job["steps"]))
    assert not any("continue-on-error" in s for s in steps), "an audit step may not fail quietly"
    gate = next(s for s in steps if "audit_gate.py" in str(s.get("run", "")))
    blocking = gate["env"]["BLOCKING"]
    assert "github.event_name != 'pull_request'" in blocking
    assert "--blocking" in gate["run"]
