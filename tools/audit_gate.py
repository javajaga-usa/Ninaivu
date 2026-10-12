"""Decide whether a dependency audit lets a build through.

``pip-audit -f json`` says which installed versions have published
advisories. This reads that report and ``requirements/audit-exceptions.toml``
(advisories somebody has looked at and accepted, each with an owner, a reason
and an expiry date) and writes the outcome as Markdown for the run's summary
page.

An advisory blocks when the build is blocking and the advisory is not
accepted, or was accepted and the acceptance has expired. Accepted ones are
still listed, with who accepted them and until when, so nothing is hidden by
being accepted. An acceptance that no longer matches anything installed is
named as stale, to be removed.

Exit status: 1 when something blocks, 2 when the inputs cannot be read, else 0.
External audit 2026-10-11, SEC-02.

    python tools/audit_gate.py audit.json requirements/audit-exceptions.toml --blocking
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REQUIRED = ("id", "package", "owner", "reason", "expires")


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    id: str
    aliases: tuple[str, ...]
    fixes: tuple[str, ...]

    def names(self) -> set[str]:
        return {self.id.upper(), *(a.upper() for a in self.aliases)}


def read_findings(report: dict[str, Any]) -> list[Finding]:
    """Each advisory once per installed package: pip-audit can list one
    twice, from two databases that know it by the same id."""
    found: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns", []) or []:
            key = (str(dep.get("name", "")).lower(), str(dep.get("version", "")),
                   str(vuln.get("id", "")).upper())
            if key in seen:
                continue
            seen.add(key)
            found.append(Finding(
                package=str(dep.get("name", "")).lower(),
                version=str(dep.get("version", "")),
                id=str(vuln.get("id", "")),
                aliases=tuple(str(a) for a in vuln.get("aliases", []) or []),
                fixes=tuple(str(f) for f in vuln.get("fix_versions", []) or [])))
    return found


def read_exceptions(text: str) -> list[dict[str, Any]]:
    """The accepted advisories. ValueError naming what is wrong with one."""
    data = tomllib.loads(text)
    accepted = data.get("exception", [])
    if not isinstance(accepted, list):
        raise ValueError("[[exception]] entries expected")
    for n, entry in enumerate(accepted, 1):
        missing = [k for k in REQUIRED if not str(entry.get(k, "")).strip()]
        if missing:
            raise ValueError(f"exception {n} ({entry.get('id', '?')}) has no {', '.join(missing)}")
        if not isinstance(entry["expires"], date):
            raise ValueError(f"exception {n} ({entry['id']}): expires must be a date, e.g. 2026-12-31")
        entry["package"] = str(entry["package"]).lower()
    return accepted


def decide(findings: list[Finding], accepted: list[dict[str, Any]], *, blocking: bool,
           today: date) -> tuple[bool, str]:
    """Whether the build is stopped, and the Markdown that says why."""
    open_, waived, expired, used = [], [], [], set()
    for f in findings:
        match = next((e for e in accepted
                      if e["package"] == f.package and str(e["id"]).upper() in f.names()), None)
        if match is None:
            open_.append(f)
            continue
        used.add(id(match))
        if match["expires"] < today:
            expired.append((f, match))
        else:
            waived.append((f, match))
    stale = [e for e in accepted if id(e) not in used]
    blocked = blocking and bool(open_ or expired)

    lines = ["### Dependency audit", ""]
    if not findings:
        lines.append("No published advisory affects an installed version.")
    if open_ or expired:
        verdict = "**Blocking.**" if blocking else "Reported, not blocking on this build."
        lines += [f"{verdict} Upgrade the package, or accept the advisory in "
                  "`requirements/audit-exceptions.toml` with an owner, a reason and an expiry.", "",
                  "| Package | Version | Advisory | Fixed in | Note |", "|---|---|---|---|---|"]
        for f in open_:
            lines.append(f"| {f.package} | {f.version} | {f.id} | {', '.join(f.fixes) or '—'} | not accepted |")
        for f, e in expired:
            lines.append(f"| {f.package} | {f.version} | {f.id} | {', '.join(f.fixes) or '—'} | "
                         f"acceptance by {e['owner']} expired {e['expires']} |")
        lines.append("")
    if waived:
        lines += ["Accepted, still present:", "",
                  "| Package | Version | Advisory | Owner | Until | Reason |", "|---|---|---|---|---|---|"]
        for f, e in waived:
            lines.append(f"| {f.package} | {f.version} | {f.id} | {e['owner']} | {e['expires']} | "
                         f"{str(e['reason']).strip()} |")
        lines.append("")
    if stale:
        lines += ["Accepted but no longer found (remove from the exceptions file): "
                  + ", ".join(f"{e['package']} {e['id']}" for e in stale), ""]
    return blocked, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", type=Path, help="pip-audit -f json output")
    parser.add_argument("exceptions", type=Path, help="requirements/audit-exceptions.toml")
    parser.add_argument("--blocking", action="store_true",
                        help="fail on an advisory that is not accepted")
    parser.add_argument("--today", type=date.fromisoformat, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        accepted = (read_exceptions(args.exceptions.read_text(encoding="utf-8"))
                    if args.exceptions.exists() else [])
    except (OSError, ValueError) as exc:
        print(f"audit gate: {exc}", file=sys.stderr)
        return 2
    blocked, text = decide(read_findings(report), accepted, blocking=args.blocking,
                           today=args.today or date.today())
    print(text)
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
