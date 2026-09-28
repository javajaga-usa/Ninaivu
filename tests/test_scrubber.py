"""The library scrubber, and what it is actually able to tell you.

The scrubber exists to answer one question: has anything in the library
changed on disk without anybody asking it to? That question can only be
answered by comparing today's fingerprint against one taken earlier, so these
tests are mostly about the comparison — including the two cases that separate
a useful integrity check from an alarming one.

Bit rot and an edit look identical if you only look at the bytes: both produce
a different hash. What tells them apart is everything *around* the bytes. A
file the user re-saved has a new modification time and usually a new size; a
file whose bytes decayed on disk has neither. Reporting the second as
corruption is the point. Reporting the first as corruption would train
somebody to ignore the warning, which is worse than not having one.
"""

import os
import time
from pathlib import Path

import pytest

from ninaivu.storage import db
from ninaivu.api.admin_api import _run_scrubber


def _asset(conn, root: Path, name: str, body: bytes) -> int:
    """Put a real file on disk and a matching row in the index."""
    path = root / name
    path.write_bytes(body)
    st = path.stat()
    cur = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, size, mtime, "
        "date_key, visibility, indexed_at) "
        "VALUES(?,?,?,'picture',?,?,'2024/01/01',1,?)",
        (str(root), name, name, st.st_size, st.st_mtime, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _latest(conn, asset_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM bitrot_records WHERE asset_id=? ORDER BY id DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    return dict(row) if row else {}


@pytest.fixture()
def library(tmp_path, cfg):
    """An indexed database with nothing in it but what each test adds."""
    conn = db.init_db(cfg.db_path)
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    return cfg, conn, root


# ---------------------------------------------------------------------------

def test_the_first_pass_records_a_baseline_rather_than_claiming_verified(library):
    """Nothing has been verified the first time a file is seen.

    There is no earlier fingerprint to compare against, so the honest answer
    is "recorded", not "verified". Saying verified here is what made the old
    scrubber useless: it reported success on a comparison it had not made.
    """
    cfg, conn, root = library
    asset_id = _asset(conn, root, "one.jpg", b"original bytes")

    _run_scrubber(cfg.db_path)

    record = _latest(conn, asset_id)
    assert record["status"] == "baseline"
    assert record["actual_hash"]
    assert record["expected_hash"] is None


def test_an_unchanged_file_verifies_on_the_second_pass(library):
    cfg, conn, root = library
    asset_id = _asset(conn, root, "one.jpg", b"original bytes")

    _run_scrubber(cfg.db_path)
    _run_scrubber(cfg.db_path)

    record = _latest(conn, asset_id)
    assert record["status"] == "verified"
    assert record["expected_hash"] == record["actual_hash"]


def test_bytes_that_change_underneath_an_untouched_file_are_corruption(library):
    """The case the scrubber exists for.

    Same size, same modification time, different bytes — nothing the user did
    could produce that, so it is decay and it must be reported.
    """
    cfg, conn, root = library
    path = root / "one.jpg"
    asset_id = _asset(conn, root, "one.jpg", b"original bytes")
    _run_scrubber(cfg.db_path)

    before = path.stat()
    path.write_bytes(b"corrupted!!!!!")   # same length as the original            # identical length
    os.utime(path, (before.st_atime, before.st_mtime))   # and identical mtime
    assert path.stat().st_size == before.st_size

    _run_scrubber(cfg.db_path)

    record = _latest(conn, asset_id)
    assert record["status"] == "corrupt"
    assert record["expected_hash"] != record["actual_hash"]


def test_a_file_the_user_edited_is_not_reported_as_corruption(library):
    """An edit changes the modification time. That is not decay.

    Getting this wrong is worse than having no scrubber: a check that cries
    corruption every time somebody rotates a photograph in another program is
    a check people learn to ignore.
    """
    cfg, conn, root = library
    path = root / "one.jpg"
    asset_id = _asset(conn, root, "one.jpg", b"original bytes")
    _run_scrubber(cfg.db_path)

    time.sleep(0.01)
    path.write_bytes(b"a genuinely edited photograph with a new length")
    os.utime(path, (time.time() + 5, time.time() + 5))

    _run_scrubber(cfg.db_path)

    record = _latest(conn, asset_id)
    assert record["status"] == "changed"

    # …and the new bytes become the baseline, so the next pass verifies
    # against the edit rather than re-reporting it for ever.
    _run_scrubber(cfg.db_path)
    assert _latest(conn, asset_id)["status"] == "verified"


def test_a_missing_file_is_reported_as_missing(library):
    cfg, conn, root = library
    path = root / "one.jpg"
    asset_id = _asset(conn, root, "one.jpg", b"original bytes")
    _run_scrubber(cfg.db_path)

    path.unlink()
    _run_scrubber(cfg.db_path)

    assert _latest(conn, asset_id)["status"] == "missing"


def test_an_unreadable_file_is_not_called_corrupt(library):
    """"I could not open it" and "the bytes changed" are different problems
    with different fixes, and conflating them sends you looking in the wrong
    place."""
    cfg, conn, root = library
    path = root / "one.jpg"
    asset_id = _asset(conn, root, "one.jpg", b"original bytes")
    _run_scrubber(cfg.db_path)

    path.chmod(0o000)
    try:
        if os.access(path, os.R_OK):        # root ignores the mode bits
            pytest.skip("running as root; permissions are not enforced")
        _run_scrubber(cfg.db_path)
        assert _latest(conn, asset_id)["status"] == "unreadable"
    finally:
        path.chmod(0o644)


def test_the_summary_counts_files_not_check_rows(library):
    """Each pass writes a row per file. Counting rows made the totals grow
    every run, so a library of three photographs could report nine verified."""
    cfg, conn, root = library
    for i in range(3):
        _asset(conn, root, f"shot{i}.jpg", f"body {i}".encode())

    _run_scrubber(cfg.db_path)
    _run_scrubber(cfg.db_path)
    _run_scrubber(cfg.db_path)

    summary = db.get_bitrot_summary(conn)
    assert summary["verified"] == 3
    assert summary["corrupt"] == 0
    assert summary["missing"] == 0


def test_a_corrupt_file_stays_corrupt_in_the_summary(library):
    cfg, conn, root = library
    path = root / "one.jpg"
    _asset(conn, root, "one.jpg", b"original bytes")
    _asset(conn, root, "two.jpg", b"another photograph")
    _run_scrubber(cfg.db_path)

    before = path.stat()
    path.write_bytes(b"corrupted!!!!!")   # same length as the original
    os.utime(path, (before.st_atime, before.st_mtime))
    _run_scrubber(cfg.db_path)

    summary = db.get_bitrot_summary(conn)
    assert summary["corrupt"] == 1
    assert summary["verified"] == 1
