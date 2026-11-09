"""Numbered migrations (storage/migrations.py): once, in order, never lowered."""
from __future__ import annotations

import sqlite3

import pytest

from ninaivu.storage import db, migrations
from ninaivu.storage.migrations import Migration, MigrationFailed


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    yield path, conn
    db.close(path)


def _make_table(name):
    def step(conn):
        conn.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
    return step


def test_every_list_is_numbered_from_one_without_a_gap():
    for steps in migrations.DATABASES.values():
        migrations.check(steps)
        assert steps[0].name == "what 3.7 has"


def test_a_gap_or_a_repeat_is_refused():
    with pytest.raises(ValueError):
        migrations.check((Migration(1, "a", _make_table("a")), Migration(3, "c", _make_table("c"))))
    with pytest.raises(ValueError):
        migrations.check((Migration(1, "a", _make_table("a")), Migration(1, "b", _make_table("b"))))


def test_a_new_index_records_the_last_step(index):
    _, conn = index
    assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)


def test_a_37_index_is_up_to_date_and_unchanged(tmp_path):
    """An index from 3.7 (no user_version) opens with nothing but the record."""
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    conn.execute("INSERT INTO meta(key, value) VALUES('marker', 'kept')")
    conn.commit()
    conn.execute("PRAGMA user_version = 0")       # as every 3.x left it
    conn.commit()
    before = conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    db.close(path)
    db._READY.discard(str(path))                    # noqa: SLF001

    conn = db.init_db(path)
    try:
        after = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        # Everything 3.7 had is there as it was; the later steps only add.
        assert {tuple(r) for r in before} <= {tuple(r) for r in after}
        added = {r[1] for r in after} - {r[1] for r in before}
        assert added <= {"copies", "idx_copies_destination", "locations"}
        assert db.get_meta(conn, "marker") == "kept"
        assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)
    finally:
        db.close(path)


def test_a_step_runs_once_in_order(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    ran = []

    def step(name):
        def apply(c):
            ran.append(name)
            c.execute(f"CREATE TABLE {name} (id INTEGER)")
        return apply

    steps = (Migration(1, "base", lambda c: None),
             Migration(2, "two", step("two")),
             Migration(3, "three", step("three")))
    assert migrations.run(conn, steps, database="test") == [1, 2, 3]
    assert ran == ["two", "three"]
    assert migrations.run(conn, steps, database="test") == []
    assert ran == ["two", "three"]
    assert migrations.recorded(conn) == 3


def test_a_failed_step_keeps_nothing_and_stops(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")

    def half_then_fail(c):
        c.execute("CREATE TABLE half (id INTEGER)")
        raise RuntimeError("disk said no")

    steps = (Migration(1, "base", lambda c: None),
             Migration(2, "fails", half_then_fail),
             Migration(3, "after", _make_table("after")))
    with pytest.raises(MigrationFailed):
        migrations.run(conn, steps, database="test")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "half" not in tables and "after" not in tables
    assert migrations.recorded(conn) == 1


def test_a_newer_record_is_never_lowered(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    steps = (Migration(1, "base", lambda c: None), Migration(2, "two", _make_table("two")))
    assert migrations.run(conn, steps, database="test") == []
    assert migrations.recorded(conn) == 9
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "two" not in tables


def test_a_new_step_changes_the_shape_an_update_copies_for(monkeypatch):
    from ninaivu.storage import upgrade
    before = upgrade.index_shape()
    monkeypatch.setattr(migrations, "INDEX",
                        migrations.INDEX + (Migration(2, "a new table", _make_table("n")),))
    monkeypatch.setitem(migrations.DATABASES, "index", migrations.INDEX)
    assert upgrade.index_shape() != before


def test_the_archive_records_its_steps(tmp_path):
    from ninaivu import archive
    from ninaivu.archive import database as archive_db
    archive.configure(tmp_path, None, False)
    archive_db.init_db()
    conn = archive_db.get_db()
    assert migrations.recorded(conn) == migrations.latest(migrations.ARCHIVE)


def test_db_still_names_everything_that_moved():
    """The visibility section lives in db_visibility; db re-exports every name in it."""
    import ast
    from pathlib import Path
    from ninaivu.storage import db_visibility
    tree = ast.parse(Path(db_visibility.__file__).read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    names |= {t.id for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    assert "set_visibility" in names and "with_companions" in names
    assert [n for n in sorted(names) if getattr(db, n, None) is not getattr(db_visibility, n)] == []


# -- what is kept with 3.x (decided 9 November 2026) ---------------------------

def _a_37_home(tmp_path):
    """A state folder as 3.7 left it: schema 6, no step recorded."""
    state = tmp_path / "state"
    state.mkdir()
    path = state / "index.db"
    conn = db.init_db(path)
    db.set_meta(conn, "household", "ours")
    conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    conn.commit()
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    db.close(path)
    db._READY.discard(str(path))                    # noqa: SLF001
    (state / "version.json").write_text('{"version": "3.7.0", "shape": "x"}', encoding="utf-8")
    return state


def test_a_37_index_upgrades_after_a_copy_is_made(tmp_path):
    from ninaivu.storage import upgrade
    state = _a_37_home(tmp_path)
    done = upgrade.prepare(state, version="4.0.0")
    assert done["from"] == "3.7.0" and done["backup"]
    conn = db.init_db(state / "index.db")
    try:
        assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
        assert db.get_meta(conn, "household") == "ours"
        assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)
    finally:
        db.close(state / "index.db")


def test_37_does_not_open_an_index_40_has_opened(tmp_path, monkeypatch):
    """3.7 knows schema 6. It refuses the index and names the copy to restore."""
    from ninaivu.storage import upgrade
    state = _a_37_home(tmp_path)
    upgrade.prepare(state, version="4.0.0")
    db.init_db(state / "index.db")                         # 4.0 opens it
    db.close(state / "index.db")
    assert db.SCHEMA_VERSION == 7
    monkeypatch.setattr(db, "SCHEMA_VERSION", 6)            # as 3.7 reads it
    before = (state / "index.db").read_bytes()
    with pytest.raises(upgrade.NewerDataError, match="restore the copy taken before that update"):
        upgrade.prepare(state, version="3.7.0")
    assert (state / "index.db").read_bytes() == before


def _version(text):
    return tuple(int(part) for part in text.split(".")[:2])


def test_the_old_module_paths_go_in_41():
    """Code that moved into ninaivu/features kept its old import path for 4.0
    only. Once the version says 4.1, every one of them must be gone."""
    from pathlib import Path
    import ninaivu
    package = Path(ninaivu.__file__).parent
    kept = sorted(str(p.relative_to(package.parent)) for p in package.rglob("*.py")
                  if "sys.modules[__name__] = " in p.read_text(encoding="utf-8"))
    assert kept, "the 4.0 names are found by the line that makes them the moved module"
    if _version(ninaivu.__version__) >= (4, 1):
        assert kept == [], "4.1 removes the old module paths: " + ", ".join(kept)
