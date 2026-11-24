"""Golden indexes: the schema of every release line we keep upgrading from.

``tests/golden_indexes/<tag>.sql`` is the index schema exactly as that
release's ``init_db`` made it (with the ``user_version`` it recorded). Each is
loaded into an empty file, a row is put in a table, and the current
Ninaivu opens it. 4.4 (and so any 4.3 or later) must have the same tables, columns, indexes and
triggers as a brand-new index, keep the rows, and record the last migration.
Anything older is refused and left byte for byte as it was. Adding a release line is dropping one more .sql file here.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ninaivu.storage import db, migrations

GOLDEN = Path(__file__).parent / "golden_indexes"
TAGS = sorted(p.stem for p in GOLDEN.glob("v*.sql"))
#: The release lines 5.0 opens: the first with the last migration step (4.3).
#: The older ones are kept to prove they are refused, untouched.
OPENS = ["v4.4.0"]
REFUSED = [t for t in TAGS if t not in OPENS]


def _shape(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every table's columns, and the names of its indexes and triggers."""
    out: dict[str, list[tuple]] = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        out[name] = [tuple(r[1:3]) for r in conn.execute(f'PRAGMA table_info("{name}")')]
    for typ in ("index", "trigger"):
        out[f"{typ}s"] = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'", (typ,)))
    return out


def _open_golden(tmp_path: Path, tag: str) -> Path:
    path = tmp_path / "index.db"
    conn = sqlite3.connect(path)
    conn.executescript((GOLDEN / f"{tag}.sql").read_text())
    conn.execute("INSERT INTO meta(key, value) VALUES('golden_marker', ?)", (tag,))
    conn.commit()
    conn.close()
    return path


def _forget(path: Path) -> None:
    db.close(path)
    db._READY.discard(str(path))                    # noqa: SLF001


def test_there_are_golden_indexes():
    assert {"v3.7.0", "v4.0.0", "v4.2.0", "v4.4.0"} <= set(TAGS)


@pytest.mark.parametrize("tag", OPENS)
def test_the_index_of_each_release_upgrades_to_the_current_shape(tmp_path, tag):
    fresh = tmp_path / "fresh" / "index.db"
    fresh.parent.mkdir()
    fresh_conn = db.init_db(fresh)
    want = _shape(fresh_conn)
    _forget(fresh)

    path = _open_golden(tmp_path, tag)
    conn = db.init_db(path)
    try:
        assert _shape(conn) == want
        assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)
        assert conn.execute(
            "SELECT value FROM meta WHERE key='golden_marker'").fetchone()[0] == tag
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        _forget(path)


@pytest.mark.parametrize("tag", REFUSED)
def test_an_older_index_is_refused_and_left_as_it_was(tmp_path, tag):
    path = _open_golden(tmp_path, tag)

    def content() -> list[str]:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    before = content()
    with pytest.raises(migrations.IndexTooOld, match="Ninaivu 4.4.0"):
        db.init_db(path)
    assert content() == before


@pytest.mark.parametrize("tag", OPENS)
def test_opening_it_a_second_time_changes_nothing(tmp_path, tag):
    path = _open_golden(tmp_path, tag)
    conn = db.init_db(path)
    first = (_shape(conn), migrations.recorded(conn))
    _forget(path)
    conn = db.init_db(path)
    try:
        assert (_shape(conn), migrations.recorded(conn)) == first
    finally:
        _forget(path)
