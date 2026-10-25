"""Eleventh audit (after 3.0.0, G-01..): two computers, the Secondary's
update, the second copy on any disk, and moved or folded library folders.

Each test names the finding it holds to; the report is
project-audit/ninaivu-audit-2026-10-25.md in the project's files.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from ninaivu.mugil import courier, free, update
from ninaivu.storage import db, nested_roots, reroot

from test_nested_library_folders import nested  # noqa: F401 - a fixture used by name
from test_second_copy_any_disk import _run as _copy_run, house  # noqa: F401 - fixture


# --- G-01: a moved library keeps what the Secondary keeps for it -------------------

# Drive-letter roots, as tests/test_reroot.py uses: read the same on every system,
# where a "/Volumes/..." root is spelled with backslashes on Windows.
OLD, NEW = r"E:\JApple Extreme SSD\Library", r"F:\JApple Extreme SSD\Library"


def _let_go_library(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "thumbs").mkdir(parents=True)
    conn = db.init_db(state / "index.db")
    for rel in ("2019/a.jpg", "2019/b.jpg"):
        conn.execute("INSERT INTO assets(root, rel_path, filename, kind, thumb) "
                     "VALUES (?,?,?,?,'')", (OLD, rel, rel.rpartition("/")[2], "picture"))
    conn.execute("INSERT INTO mugil_let_go (root, rel_path, size, mtime, algo, digest, let_go_at) "
                 "VALUES (?, '2019/a.jpg', 3, 1.0, 'blake3', 'abc', 2.0)", (OLD,))
    conn.commit()
    conn.close()
    with courier.sent_record(state) as sent:
        sent.execute("INSERT INTO sent (root, rel_path, size, mtime, algo, digest, sent_at) "
                     "VALUES (?, '2019/a.jpg', 3, 1.0, 'blake3', 'abc', 2.0)", (OLD,))
        sent.execute("INSERT INTO libraries (root, library, name) VALUES (?, 'lib-1', 'Library')",
                     (OLD,))
    return state


def test_a_moved_library_keeps_its_let_go_originals(tmp_path):
    state = _let_go_library(tmp_path)
    reroot.reroot(state, OLD, NEW, dry_run=False)
    conn = db.connect(state / "index.db")
    assert [tuple(r) for r in conn.execute("SELECT root, rel_path FROM mugil_let_go")] == \
        [(NEW, "2019/a.jpg")]
    # Not counted as missing at the new place, so a scan does not prune it.
    db.delete_missing(conn, NEW, ["2019/b.jpg"])
    assert conn.execute("SELECT 1 FROM assets WHERE root=? AND rel_path='2019/a.jpg'",
                        (NEW,)).fetchone()
    with courier.sent_record(state) as sent:
        assert [tuple(r) for r in sent.execute("SELECT root, rel_path FROM sent")] == \
            [(NEW, "2019/a.jpg")]
        assert [tuple(r) for r in sent.execute("SELECT root, library FROM libraries")] == \
            [(NEW, "lib-1")]
    # And it can still be sent for: the record the Primary asks the Secondary by.
    from ninaivu.mugil import fetch
    assert fetch.record_of(state, NEW, "2019/a.jpg")["library"] == "lib-1"
    assert fetch.record_of(state, OLD, "2019/a.jpg") is None


def test_the_let_go_record_is_in_the_moved_tables():
    assert "mugil_let_go" in reroot.KEYED_ROOT_TABLES
    assert set(nested_roots._KEYED) == set(reroot.KEYED_ROOT_TABLES)   # noqa: SLF001


# --- G-02: a folder with let-go originals is not folded while they are away ---------

def _let_go(conn, root: str, rel: str) -> None:
    conn.execute("INSERT INTO mugil_let_go (root, rel_path, size, mtime, algo, digest, let_go_at) "
                 "VALUES (?,?,1,1.0,'blake3','x',1.0)", (root, rel))
    conn.commit()


def test_folding_refuses_while_originals_are_only_on_the_secondary(nested):  # noqa: F811
    conn = nested["conn"]
    _let_go(conn, nested["archive"], "2024/01/02/p0.jpg")
    with pytest.raises(nested_roots.HeldOnTheSecondary):
        nested_roots.fold(conn, nested["archive"], nested["master"])
    assert nested_roots.let_go_in(conn, nested["archive"]) == 1


def test_removing_a_folder_brings_its_originals_back_first(nested, monkeypatch):  # noqa: F811
    conn = nested["conn"]
    _let_go(conn, nested["archive"], "2024/01/02/p0.jpg")
    # The Secondary is away: nothing comes back, and nothing is removed.
    monkeypatch.setattr(free, "bring_back", lambda cfg, conn, root, rel, **kw: False)
    response = nested["console"].delete("/api/admin/libraries",
                                        query_string={"path": nested["archive"], "force": 1})
    assert response.status_code == 409 and response.get_json()["held"] == 1
    assert nested["archive"] in nested["cfg"].roots

    def back(cfg, conn, root, rel, **kw):
        conn.execute("DELETE FROM mugil_let_go WHERE root=? AND rel_path=?", (root, rel))
        conn.commit()
        return True

    monkeypatch.setattr(free, "bring_back", back)
    response = nested["console"].delete("/api/admin/libraries",
                                        query_string={"path": nested["archive"], "force": 1})
    assert response.status_code == 200, response.get_json()
    assert conn.execute("SELECT 1 FROM assets WHERE root=? AND rel_path=?",
                        (nested["master"], "Ninaivu Archive/2024/01/02/p0.jpg")).fetchone()


# --- G-03: an update left half-way does not refuse every later one ------------------

@pytest.fixture
def updater(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    folder = type("Folder", (), {"path": tmp_path / "Ninaivu-3.0.0-linux-x64-portable"})()
    return update.Updater(state, folder=folder, quiet=lambda: True, systemd=False,
                          server_args=[], spawn=lambda *a, **k: None, version="3.0.1",
                          machine="x86_64")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="only a Linux Secondary updates")
def test_a_slow_start_that_served_is_done(updater):
    # The helper stopped watching (slow) and the newer version came up later.
    update.note(updater.state, "starting", to="3.0.1", message="not finished starting yet")
    assert updater.refusal() is None
    assert update.record(updater.state)["stage"] == "done"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="only a Linux Secondary updates")
def test_a_hand_over_that_never_happened_is_given_up(updater, monkeypatch):
    update.note(updater.state, "switching", to="3.0.2", message="")
    assert "under way" in updater.refusal()
    later = time.time() + update.HANDED_OVER_LOST + 60
    monkeypatch.setattr(update.time, "time", lambda: later)
    assert updater.refusal() is None
    said = update.record(updater.state)
    assert said["stage"] == "failed" and "did not finish" in said["message"]


# --- G-04: a second-copy name another file was given is not written over ------------

def test_a_file_named_as_another_was_stored_gets_a_name_of_its_own(house, scanned):  # noqa: F811
    if not house["conn"].execute("SELECT 1 FROM assets WHERE rel_path='trips/what?.jpg'"
                                 ).fetchone():
        pytest.skip("this system's own disk cannot hold the name either")
    _copy_run(house, "exfat")
    copy = house["disk"] / house["library"].name / "trips"
    assert (copy / "what.jpg").read_bytes() == b"a question"
    # Later a real "what.jpg" is added to the library.
    (house["library"] / "trips" / "what.jpg").write_bytes(b"an answer")
    scanned[2]._run(house["library"], full=True)
    status = _copy_run(house, "exfat")
    assert not status["error"], status
    assert (copy / "what.jpg").read_bytes() == b"a question", \
        "the copy of what?.jpg was set aside and written over"
    stored = dict(tuple(r) for r in house["conn"].execute(
        "SELECT rel_path, stored_as FROM mirror_copies WHERE rel_path LIKE 'trips/what%'"))
    lib = house["library"].name
    assert stored["trips/what?.jpg"] == f"{lib}/trips/what.jpg"
    assert stored["trips/what.jpg"] == f"{lib}/trips/what (2).jpg"
    assert (copy / "what (2).jpg").read_bytes() == b"an answer"
    assert not [p for p in copy.iterdir() if "before" in p.name]
