"""The archive describes itself, on the drive, for whoever plugs it in next.

Ninaivu's record of an archive lives on the computer that ran it. These check
that after a real run, and after an audit, the drive carries its own snapshot
page, manifest, portable checksums and a verifier that works with nothing but
Python -- and that a dry run writes none of it, and a failure to write it never
fails the copy.
"""
import csv
import hashlib
import json
import os
import subprocess
import sys

from test_archive_course_material import a_course, a_trip, camera_jpeg, write  # noqa: F401
from test_archive_course_material import drive as drive                           # noqa: F401
from ninaivu.archive import database as db
from ninaivu.archive import scanner, status_kit
from ninaivu.archive.scanner import ArchiveJob

KIT = status_kit.KIT_FILES


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(root, dest, mode=scanner.MODE_COPY):
    job = ArchiveJob([str(root)], str(dest), mode=mode)
    job.run()
    return job


def test_a_real_run_leaves_the_kit_at_the_root_of_the_archive(drive):
    root, dest = drive
    a_trip(root / "Photos")
    run(root, dest)

    for name in KIT:
        assert (dest / name).is_file(), name
    assert not list(dest.glob("*.writing")), "no half-written file left behind"

    with open(dest / status_kit.MANIFEST, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4, "the trip has four photos"
    for row in rows:
        assert row["kind"] == "photo" and row["captured"].startswith("2019")
        assert sha256(dest / row["path"]) == row["sha256"]

    report = json.loads((dest / status_kit.RECOVERY_REPORT).read_text(encoding="utf-8"))
    assert report["format"] == "ninaivu-recovery-v1" and len(report["files"]) == 4

    page = (dest / status_kit.STATUS_PAGE).read_text(encoding="utf-8")
    assert "Status as of" in page and "not live" in page
    assert ">4</dd>" in page, "four photos counted"
    assert "2019" in page and "Never audited" in page


def test_the_verifier_on_the_drive_needs_nothing_but_python(drive):
    """Run from the archive's root with no arguments, the way a person would on
    another computer, years later -- no Ninaivu on its import path."""
    root, dest = drive
    a_trip(root / "Photos")
    run(root, dest)

    clean_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    ok = subprocess.run([sys.executable, "-I", status_kit.VERIFIER], cwd=dest,
                        capture_output=True, text=True, env=clean_env, timeout=120)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "4 matched, 0 missing, 0 changed" in ok.stdout

    photo = next(p for p in dest.rglob("IMG_*.JPG"))
    photo.write_bytes(photo.read_bytes() + b"bit rot")
    bad = subprocess.run([sys.executable, "-I", status_kit.VERIFIER], cwd=dest,
                         capture_output=True, text=True, env=clean_env, timeout=120)
    assert bad.returncode == 1 and "CHANGED:" in bad.stdout and photo.name in bad.stdout


def test_an_audit_refreshes_the_page_with_what_it_found(drive):
    root, dest = drive
    a_trip(root / "Photos")
    run(root, dest)
    photo = next(p for p in dest.rglob("IMG_*.JPG"))
    photo.write_bytes(photo.read_bytes() + b"changed")

    run(root, dest, mode=scanner.MODE_VERIFY)
    page = (dest / status_kit.STATUS_PAGE).read_text(encoding="utf-8")
    assert "Last audit" in page and "1 changed" in page
    assert 'class="check warn"' in page, "a failed audit is shown as needing attention"
    assert "need attention" in page


def test_a_clean_audit_is_shown_as_clean(drive):
    root, dest = drive
    a_trip(root / "Photos")
    run(root, dest)
    run(root, dest, mode=scanner.MODE_VERIFY)
    page = (dest / status_kit.STATUS_PAGE).read_text(encoding="utf-8")
    assert 'class="check ok"' in page and "0 changed, 0 missing" in page


def test_a_dry_run_writes_nothing_its_description_included(drive):
    root, dest = drive
    a_trip(root / "Photos")
    run(root, dest, mode=scanner.MODE_DRY_RUN)
    assert not any((dest / name).exists() for name in KIT)


def test_held_course_folders_are_listed_and_survive_an_audit(drive):
    root, dest = drive
    a_course(root / "Materials", "[FreeTutorials.Us] Tips & Tricks course")
    a_trip(root / "Photos")
    run(root, dest)
    page = (dest / status_kit.STATUS_PAGE).read_text(encoding="utf-8")
    assert "Tips &amp; Tricks course" in page, "names are escaped, not injected"
    assert "held back" in page
    run(root, dest, mode=scanner.MODE_VERIFY)
    assert "Tips &amp; Tricks course" in (dest / status_kit.STATUS_PAGE).read_text(encoding="utf-8")


def test_failing_to_write_the_kit_never_fails_the_copy(drive, monkeypatch):
    root, dest = drive
    a_trip(root / "Photos")
    lines = []

    def refuse(*_a, **_k):
        raise PermissionError("the drive is write-protected")

    monkeypatch.setattr(status_kit, "write_status_kit", refuse)
    job = ArchiveJob([str(root)], str(dest))
    monkeypatch.setattr(job, "log", lambda line, _orig=job.log: (lines.append(line), _orig(line)))
    job.run()
    assert db.get_job(job.job_id)["state"] == "completed"
    assert sum(1 for r in db.get_recent_files(100) if r["status"] == "verified") == 4
    assert any("STATUS NOT WRITTEN" in line and "write-protected" in line for line in lines), lines
