"""A description of the archive, written onto the archive itself.

Ninaivu's record of an archive -- which files were verified, their checksums,
what went wrong, which folders were held back -- lives in its state folder on
the computer that ran it. Unplug the drive and that record stays behind: the
drive alone says nothing about itself. A family archive should outlive the
laptop, the program and whoever set it up.

So after every real archive run and every audit Ninaivu leaves four files at the
root of the archive:

``ARCHIVE STATUS.html``
    What is on the drive and how it last checked out, readable in any browser,
    offline, on any computer. A snapshot, dated prominently -- never live.
``archive-manifest.csv``
    Every archived file with its size, capture date and SHA-256, for a
    spreadsheet or a person.
``ninaivu-recovery.json``
    The same checksums in Ninaivu's portable recovery format.
``verify-archive.py``
    Re-checks every file against that report using only Python's standard
    library, with no Ninaivu installed.

Each file is written beside itself and then swapped into place, so a power cut
leaves the previous version rather than half a page. Writing the kit never
fails a run: an archive that copied correctly is not made wrong by its
description.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from . import database as db
from .safety import long_path, normalise

STATUS_PAGE = "ARCHIVE STATUS.html"
MANIFEST = "archive-manifest.csv"
RECOVERY_REPORT = "ninaivu-recovery.json"
VERIFIER = "verify-archive.py"
KIT_FILES = (STATUS_PAGE, MANIFEST, RECOVERY_REPORT, VERIFIER)

_COURSE_KEY = "status-kit:course-folders:"
_KIND_BY_EXT = {
    **{e: "photo" for e in ("jpg", "jpeg", "heic", "heif", "png", "gif", "webp", "tif", "tiff",
                            "bmp", "dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw")},
    **{e: "video" for e in ("mp4", "mov", "m4v", "avi", "mkv", "wmv", "mts", "m2ts", "3gp",
                            "mpg", "mpeg", "webm", "flv", "ts")},
    **{e: "audio" for e in ("mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "wma", "amr")},
}


def _kind(path: str) -> str:
    return _KIND_BY_EXT.get(path.rsplit(".", 1)[-1].lower() if "." in path else "", "other")


def _relative(path: str, root: str) -> str | None:
    """*path* inside *root*, with forward slashes; None when it is not inside."""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:                        # another drive on Windows
        return None
    if relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return None
    return relative.replace("\\", "/")


def archived_files(destination: str) -> Iterable[dict[str, Any]]:
    """Verified files that belong to this archive, in path order."""
    for row in db.iter_recovery_files(destination):
        relative = _relative(row["destination_path"], destination)
        if relative is None:
            continue
        yield {"path": relative, "size": row["size"] or 0, "sha256": row["file_hash"],
               "captured": row["exif_date"] or ""}


def remember_course_folders(destination: str, folders: list[dict[str, Any]]) -> None:
    """Keep the last run's set-aside folders, so an audit's page still lists them.

    Courses, and the films and music held back file by file, with their category.
    """
    kept = [{"path": f["path"], "state": f["state"], "reasons": f.get("reasons", []),
             "category": f.get("category", "course"), "files": f.get("files"),
             "spared": f.get("spared", 0)}
            for f in folders if f.get("state") in ("held", "excluded", "review")]
    db.set_config(_COURSE_KEY + normalise(destination), json.dumps(kept))


def _course_folders(destination: str) -> list[dict[str, Any]]:
    try:
        return json.loads(db.get_config(_COURSE_KEY + normalise(destination), "[]") or "[]")
    except ValueError:
        return []


def _jobs_for(destination: str) -> tuple[dict | None, dict | None]:
    """(last archive run, last audit) that wrote to this archive."""
    mine = normalise(destination)
    last_run = last_audit = None
    for row in db.get_db().execute(
            "SELECT * FROM jobs WHERE state='completed' ORDER BY id DESC LIMIT 400"):
        job = dict(row)
        if not job.get("destination") or normalise(job["destination"]) != mine:
            continue
        if job.get("mode") == "verify" and last_audit is None:
            last_audit = job
        elif job.get("mode") == "copy" and last_run is None:
            last_run = job
        if last_run and last_audit:
            break
    return last_run, last_audit


def _attention(destination: str, limit: int = 100) -> tuple[int, list[dict[str, str]]]:
    """The files that failed on their way to *this* drive.

    Every other figure on the page is this drive's, and so is this one: the
    page is left on the drive, which may be handed to a relative, and it used
    to list the paths and errors of every archive run this computer ever made.
    """
    conn = db.get_db()
    total = 0
    found: list[dict[str, str]] = []
    for row in conn.execute("SELECT source_path, destination_path, error FROM files "
                            "WHERE status='error' AND COALESCE(destination_path, '') <> '' "
                            "ORDER BY updated_at DESC"):
        if _relative(row["destination_path"], destination) is None:
            continue
        total += 1
        if len(found) < limit:
            found.append(dict(row))
    return total, found


def _write_atomically(path: Path, write) -> None:
    temporary = path.with_name(path.name + ".writing")
    try:
        with open(long_path(str(temporary)), "w", encoding="utf-8", newline="") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(long_path(str(temporary)), long_path(str(path)))
    except BaseException:
        # A full archive disk used to leave the half-written list in the
        # archive's root, beside the files a person is meant to read.
        _remove_quietly(temporary)
        raise


def _remove_quietly(path: Path) -> None:
    try:
        os.remove(long_path(str(path)))
    except OSError:
        pass


def write_status_kit(destination: str, *, now: float | None = None) -> dict[str, Any]:
    """Write the four files to the root of *destination* and return what was counted."""
    now = time.time() if now is None else now
    root = Path(destination)
    totals = {"photo": [0, 0], "video": [0, 0], "audio": [0, 0], "other": [0, 0]}
    years: dict[str, dict[str, int]] = {}

    manifest_path, report_path = root / MANIFEST, root / RECOVERY_REPORT

    def write_manifest_and_report(manifest_handle):
        writer = csv.writer(manifest_handle, lineterminator="\n")
        writer.writerow(["path", "kind", "size_bytes", "captured", "sha256"])
        temporary = report_path.with_name(report_path.name + ".writing")
        try:
            with open(long_path(str(temporary)), "w", encoding="utf-8", newline="") as report:
                header = {"format": "ninaivu-recovery-v1",
                          "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                          "archive_root": root.name}
                report.write(json.dumps(header, separators=(",", ":"))[:-1] + ',"files":[')
                first = True
                for item in archived_files(destination):
                    kind = _kind(item["path"])
                    totals[kind][0] += 1
                    totals[kind][1] += item["size"]
                    year = item["captured"][:4] if item["captured"][:4].isdigit() else "Undated"
                    years.setdefault(year, {"photo": 0, "video": 0, "audio": 0, "other": 0})[kind] += 1
                    writer.writerow([item["path"], kind, item["size"], item["captured"], item["sha256"]])
                    report.write(("" if first else ",") + json.dumps(
                        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]},
                        separators=(",", ":")))
                    first = False
                report.write("]}")
                report.flush()
                os.fsync(report.fileno())
            os.replace(long_path(str(temporary)), long_path(str(report_path)))
        except BaseException:
            _remove_quietly(temporary)
            raise

    _write_atomically(manifest_path, write_manifest_and_report)

    verifier = Path(__file__).with_name("verify_archive.py").read_text(encoding="utf-8")
    _write_atomically(root / VERIFIER, lambda handle: handle.write(verifier))

    try:
        usage = shutil.disk_usage(long_path(str(root)))
        disk = {"total": usage.total, "free": usage.free}
    except OSError:
        disk = None
    last_run, last_audit = _jobs_for(destination)
    errors, attention = _attention(destination)
    facts = {"generated": now, "root": str(root), "totals": totals, "years": years,
             "disk": disk, "last_run": last_run, "last_audit": last_audit,
             "errors": errors, "attention": attention,
             "course_folders": _course_folders(destination)}
    page = render_status_page(facts)
    _write_atomically(root / STATUS_PAGE, lambda handle: handle.write(page))
    return facts


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def _bytes(n: int | None) -> str:
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _when(stamp: float | None) -> str:
    if not stamp:
        return "never"
    return time.strftime("%d %B %Y, %H:%M", time.localtime(stamp)).lstrip("0")


def render_status_page(facts: dict[str, Any]) -> str:
    e = html.escape
    totals = facts["totals"]
    archived = sum(n for n, _ in totals.values())
    size = sum(b for _, b in totals.values())
    disk = facts["disk"]
    audit, run = facts["last_audit"], facts["last_run"]

    if audit:
        counts = re.search(r"(\d+) changed, (\d+) missing", audit.get("message") or "")
        clean = counts is not None and counts.group(1) == "0" and counts.group(2) == "0"
        audit_state = "ok" if clean else "warn"
        audit_line = (f"<strong>Last audit {e(_when(audit.get('ended_at')))}</strong>"
                      f"<span>{e(audit.get('message') or '')}</span>")
    else:
        audit_state = "none"
        audit_line = ("<strong>Never audited</strong><span>Run <em>Audit archive</em> in Ninaivu, "
                      "or <code>python verify-archive.py</code>, to re-check every file.</span>")

    run_line = (f"<strong>Last archive run {e(_when(run.get('ended_at')))}</strong>"
                f"<span>{e(run.get('message') or '')}</span>") if run else \
        "<strong>No completed archive run recorded</strong><span></span>"

    figures = "".join(
        f"<div class=\"figure\"><dt>{label}</dt><dd>{totals[kind][0]:,}</dd>"
        f"<small>{_bytes(totals[kind][1])}</small></div>"
        for kind, label in (("photo", "Photos"), ("video", "Videos"), ("audio", "Audio")))
    figures += (f"<div class=\"figure\"><dt>Archived</dt><dd>{_bytes(size)}</dd>"
                f"<small>{archived:,} files</small></div>")
    if disk:
        figures += (f"<div class=\"figure\"><dt>Free on drive</dt><dd>{_bytes(disk['free'])}</dd>"
                    f"<small>of {_bytes(disk['total'])}</small></div>")

    year_rows = "".join(
        f"<tr><th scope=\"row\">{e(year)}</th><td>{counts['photo']:,}</td><td>{counts['video']:,}</td>"
        f"<td>{counts['audio']:,}</td><td>{sum(counts.values()):,}</td></tr>"
        for year, counts in sorted(facts["years"].items(), key=lambda kv: (kv[0] == "Undated", kv[0])))

    if facts["errors"]:
        items = "".join(
            f"<li><code>{e(row.get('destination_path') or row.get('source_path') or '')}</code>"
            f"<span>{e(row.get('error') or '')}</span></li>" for row in facts["attention"])
        more = facts["errors"] - len(facts["attention"])
        attention = (f"<details><summary>{facts['errors']:,} file(s) need attention</summary>"
                     f"<ul class=\"list\">{items}</ul>"
                     + (f"<p class=\"note\">…and {more:,} more. Ninaivu’s Archive tab lists them all.</p>" if more > 0 else "")
                     + "</details>")
    else:
        attention = "<p class=\"quiet\">Nothing needs attention.</p>"

    courses = facts["course_folders"]
    labels = {"held": "held back", "excluded": "excluded", "review": "copied, has camera photos"}
    kinds = {"course": "course material", "film": "films and TV", "music": "music"}

    def describe(c: dict[str, Any]) -> str:
        category = c.get("category", "course")
        state = labels.get(c["state"], c["state"])
        if category != "course":
            state = ("copied, looks personal" if c["state"] == "review"
                     else f"{c.get('files') or 0} files {state}")
            if c.get("spared") and c["state"] != "review":
                state += f", {c['spared']} personal files copied"
        return f"{kinds.get(category, category)}: {state}"

    course_html = ("<ul class=\"list\">" + "".join(
        f"<li><code>{e(c['path'])}</code><span>{e(describe(c))} — "
        f"{e('; '.join(c.get('reasons') or []))}</span></li>" for c in courses) + "</ul>") if courses else \
        "<p class=\"quiet\">None were set aside in the last run.</p>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archive status — {e(Path(facts['root']).name or facts['root'])}</title>
<style>
:root {{
  --paper: #F7F5F0; --panel: #FFFFFF; --ink: #22201C; --ink-2: #5E5A52; --rule: #DDD8CE;
  --ok: #2F6B4A; --ok-soft: #E1EEE6; --warn: #9A4B14; --warn-soft: #F5E6D6; --accent: #3D5A80;
  --serif: Georgia, "Iowan Old Style", "Palatino Linotype", "Times New Roman", serif;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "Cascadia Mono", Consolas, "Liberation Mono", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --paper: #171614; --panel: #201F1C; --ink: #ECE8E1; --ink-2: #ABA59B; --rule: #34312C;
          --ok: #7CC39C; --ok-soft: #1E3328; --warn: #E3A46A; --warn-soft: #3A2A1C; --accent: #9DB7D9; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.55 var(--sans);
        padding-inline: 16px; padding-block: 32px 64px; }}
main {{ max-width: 880px; margin: 0 auto; display: flex; flex-direction: column; gap: 28px; }}
header {{ display: flex; flex-direction: column; gap: 8px; border-bottom: 3px double var(--rule); padding-bottom: 18px; }}
.label {{ font: 600 12px/1 var(--sans); letter-spacing: .14em; text-transform: uppercase; color: var(--ink-2); }}
h1 {{ margin: 0; font: 400 clamp(32px, 6vw, 46px)/1.05 var(--serif); text-wrap: balance; }}
h2 {{ margin: 0 0 10px; font: 400 22px/1.2 var(--serif); }}
.stamp {{ display: inline-flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; font-size: 14px; color: var(--ink-2); }}
.stamp strong {{ color: var(--ink); font-weight: 600; }}
dl.figures {{ margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px;
              background: var(--rule); border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; }}
.figure {{ background: var(--panel); padding: 14px 16px; display: flex; flex-direction: column; gap: 2px; }}
.figure dt {{ font: 600 12px/1 var(--sans); letter-spacing: .08em; text-transform: uppercase; color: var(--ink-2); }}
.figure dd {{ margin: 4px 0 0; font: 400 30px/1.1 var(--serif); font-variant-numeric: tabular-nums; }}
.figure small {{ color: var(--ink-2); font-size: 13px; }}
.checks {{ display: grid; gap: 10px; }}
.check {{ display: flex; flex-direction: column; gap: 2px; padding: 12px 14px; border-radius: 6px; border: 1px solid var(--rule); background: var(--panel); }}
.check span {{ color: var(--ink-2); font-size: 14.5px; }}
.check.ok {{ border-color: var(--ok); background: var(--ok-soft); }}
.check.warn, .check.none {{ border-color: var(--warn); background: var(--warn-soft); }}
.scroll {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 7px 10px; border-bottom: 1px solid var(--rule); text-align: right; }}
thead th {{ font: 600 12px/1 var(--sans); letter-spacing: .08em; text-transform: uppercase; color: var(--ink-2); }}
th[scope=row], thead th:first-child {{ text-align: left; }}
.list {{ list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }}
.list li {{ display: flex; flex-direction: column; gap: 2px; }}
.list span, .quiet, .note {{ color: var(--ink-2); font-size: 14.5px; margin: 0; }}
code {{ font: 13.5px/1.4 var(--mono); overflow-wrap: anywhere; }}
summary {{ cursor: pointer; font-weight: 600; }}
.howto p {{ margin: 0 0 8px; max-width: 68ch; }}
.howto pre {{ margin: 0 0 10px; padding: 10px 12px; background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; overflow-x: auto; }}
</style>
</head>
<body>
<main>
  <header>
    <span class="label">Family archive · written by Ninaivu</span>
    <h1>{e(Path(facts['root']).name or facts['root'])}</h1>
    <p class="stamp"><strong>Status as of {e(_when(facts['generated']))}</strong>
      <span>A snapshot written after the last archive run or audit — not live. Anything changed on the drive since then is not reflected here.</span></p>
  </header>

  <section aria-label="What is archived"><dl class="figures">{figures}</dl></section>

  <section>
    <h2>How it last checked out</h2>
    <div class="checks">
      <div class="check {audit_state}">{audit_line}</div>
      <div class="check">{run_line}</div>
    </div>
  </section>

  <section>
    <h2>Needs attention</h2>
    {attention}
  </section>

  <section>
    <h2>Set aside: courses, films and music</h2>
    {course_html}
  </section>

  <section>
    <h2>By year</h2>
    <div class="scroll"><table>
      <thead><tr><th>Year</th><th>Photos</th><th>Videos</th><th>Audio</th><th>All files</th></tr></thead>
      <tbody>{year_rows or '<tr><td colspan="5">Nothing archived yet.</td></tr>'}</tbody>
    </table></div>
  </section>

  <section class="howto">
    <h2>Checking this drive without Ninaivu</h2>
    <p><code>{MANIFEST}</code> lists every archived file with its size, capture date and SHA-256 checksum; it opens in any spreadsheet.
       <code>{RECOVERY_REPORT}</code> holds the same checksums for the checker.</p>
    <p>To confirm nothing on the drive has changed or gone missing, open a terminal in this folder and run, with Python 3 installed:</p>
    <pre><code>python {VERIFIER}</code></pre>
    <p>It needs nothing else, prints how many files matched, and names any that are missing or different.</p>
  </section>
</main>
</body>
</html>
"""
