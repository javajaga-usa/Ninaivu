"""Starting a new version over a home an older one set up (storage/upgrade.py).

An update replaces the program only. What the first start of a new version
may change is the index, and that change cannot be taken back by installing
the old version again; so the state folder is copied first, and an index a
newer version wrote is not opened at all.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest

from ninaivu.archive import database as archive_db
from ninaivu.storage import backup, db, upgrade


def _home(tmp_path: Path) -> Path:
    """A state folder an earlier version used: an index with something in it."""
    state = tmp_path / "state"
    state.mkdir()
    conn = db.init_db(state / "index.db")
    db.set_meta(conn, "household", "ours")
    conn.commit()
    (state / "config.json").write_text('{"roots": ["/photos"]}', encoding="utf-8")
    return state


def _manifest(bundle: str) -> dict:
    with tarfile.open(bundle) as tar:
        return json.load(tar.extractfile("state/backup_manifest.json"))


def test_a_new_home_needs_no_copy(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    done = upgrade.prepare(state, version="2.0.0")
    assert done == {"from": None, "to": "2.0.0", "backup": None}
    assert upgrade.recorded(state)["version"] == "2.0.0"
    assert not (state / upgrade.BEFORE_UPDATE).exists()


def test_the_first_start_of_a_new_version_copies_the_state_first(tmp_path):
    state = _home(tmp_path)
    upgrade.prepare(state, version="1.0.5")       # as from 1.0.4, which kept no record
    record = upgrade.recorded(state)
    assert record["version"] == "1.0.5" and record["previous"] is None
    bundles = backup.bundles(state / upgrade.BEFORE_UPDATE)
    assert len(bundles) == 1
    assert _manifest(bundles[0]["path"])["note"] == {
        "before_update": {"from": "1.0.4 or earlier", "to": "1.0.5"}}

    # The same version again: nothing more.
    assert upgrade.prepare(state, version="1.0.5")["backup"] is None
    done = upgrade.prepare(state, version="1.0.6")
    assert done["from"] == "1.0.5" and done["backup"]
    assert _manifest(done["backup"])["note"]["before_update"]["from"] == "1.0.5"
    with closing(sqlite3.connect(str(state / "index.db"))) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='household'").fetchone()[0] == "ours"


def test_the_copy_restores_to_what_was_there(tmp_path):
    state = _home(tmp_path)
    done = upgrade.prepare(state, version="1.0.5")
    out = tmp_path / "out"
    backup.extract(Path(done["backup"]), out)
    restored = next(out.rglob("index.db"))
    with closing(sqlite3.connect(str(restored))) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='household'").fetchone()[0] == "ours"
    assert next(out.rglob("config.json")).read_text(encoding="utf-8") == '{"roots": ["/photos"]}'


def test_only_the_last_few_copies_are_kept(tmp_path):
    state = _home(tmp_path)
    for n in range(upgrade.KEEP + 2):
        upgrade.prepare(state, version=f"1.1.{n}")
    assert len(backup.bundles(state / upgrade.BEFORE_UPDATE)) == upgrade.KEEP


def test_an_index_a_newer_version_wrote_is_not_opened(tmp_path):
    state = _home(tmp_path)
    upgrade.prepare(state, version="3.0.0")
    with closing(sqlite3.connect(str(state / "index.db"))) as conn:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                     (str(db.SCHEMA_VERSION + 1),))
        conn.commit()
    before = (state / "index.db").read_bytes()
    with pytest.raises(upgrade.NewerDataError, match="Ninaivu 3.0.0"):
        upgrade.prepare(state, version="2.0.0")
    assert (state / "index.db").read_bytes() == before
    assert upgrade.recorded(state)["version"] == "3.0.0"


def test_an_archive_record_a_newer_version_wrote_is_not_opened(tmp_path):
    state = _home(tmp_path)
    with closing(sqlite3.connect(str(state / "archive.db"))) as conn:
        conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO config VALUES('schema_version', ?)",
                     (str(archive_db.SCHEMA_VERSION + 1),))
        conn.commit()
    with pytest.raises(upgrade.NewerDataError):
        upgrade.prepare(state, version="2.0.0")


def test_no_copy_no_start(tmp_path, monkeypatch):
    state = _home(tmp_path)
    monkeypatch.setattr(backup, "snapshot", lambda *a, **k: None)
    with pytest.raises(upgrade.UpdateBackupError):
        upgrade.prepare(state, version="2.0.0", environ={})
    assert upgrade.recorded(state) is None, "so the next start tries again"
    assert not upgrade.before_opening_the_index(state)
    # Unless the person said to start without one.
    done = upgrade.prepare(state, version="2.0.0", environ={upgrade.SKIP_ENV: "1"})
    assert done["backup"] is None and upgrade.recorded(state)["version"] == "2.0.0"


def test_an_older_version_never_lowers_the_schema_number(tmp_path):
    path = tmp_path / "index.db"
    db.init_db(path)
    with closing(sqlite3.connect(str(path))) as raw:
        raw.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(db.SCHEMA_VERSION + 3),))
        raw.commit()
    conn = db.init_db(path)
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION + 3)


def test_the_server_stops_before_the_index_when_it_must(tmp_path, monkeypatch, capsys):
    from ninaivu import __main__ as entry

    state = _home(tmp_path)
    with closing(sqlite3.connect(str(state / "index.db"))) as conn:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                     (str(db.SCHEMA_VERSION + 1),))
        conn.commit()
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(state))
    ran = []
    monkeypatch.setattr(entry, "_run", lambda cfg, args: ran.append(cfg) or 0)
    assert entry.main(["--no-admin"]) == 1
    assert not ran
    assert "does not know" in capsys.readouterr().err
