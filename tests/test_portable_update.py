"""Updating a portable Ninaivu: the newer folder takes the library over.

desktop/portable.py: a 1.12 folder keeps its data apart from the program, a
newer folder unpacked beside an older one brings the older one's library
across (moved, not copied), and "Go back" hands it back as the older version
left it, using the copy of the index the newer version's first start took.
These run on folders laid out as the Linux portable build lays them out; the
Windows one is the same with Ninaivu.exe in place of the script.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from ninaivu.desktop import app as panel
from ninaivu.desktop import portable
from ninaivu.storage import upgrade

pytestmark = pytest.mark.skipif(sys.platform in ("win32", "darwin"),
                                reason="lays folders out as the Linux portable build does")


def _index(path: Path, words: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS assets (id INTEGER PRIMARY KEY, words TEXT)")
        conn.execute("DELETE FROM assets")
        conn.execute("INSERT INTO assets(words) VALUES (?)", (words,))
        conn.commit()


def _words(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT words FROM assets").fetchone()[0]


def _folder(parent: Path, name: str, *, layout: str = "1.12", version: str | None = None,
            words: str | None = None) -> Path:
    """A Linux portable folder: the launcher script, app/python, and a library
    where *layout* kept it."""
    path = parent / name
    (path / "app" / "python" / "bin").mkdir(parents=True)
    (path / "ninaivu").write_text("#!/bin/sh\n")
    (path / "logs").mkdir()
    if words is not None:
        if layout == "1.11":
            state, models, control = (path / "app" / "data", path / "app" / "ai-models",
                                      path / "app" / ".ninaivu-control")
        else:
            state = path / "data"
            models, control = state / "ai-models", state / "control" / ".ninaivu-control"
        _index(state / "index.db", words)
        (state / "thumbs" / "2024").mkdir(parents=True)
        (state / "thumbs" / "2024" / "a.webp").write_bytes(b"thumb")
        (state / "config.json").write_text(json.dumps({"roots": ["/photos"]}))
        (models / "clip").mkdir(parents=True)
        (models / "clip" / "model.onnx").write_bytes(b"weights")
        control.mkdir(parents=True)
        (control / "settings.json").write_text('{"mode": "performance"}')
        if version:
            (state / upgrade.VERSION_FILE).write_text(json.dumps({"version": version}))
    return path


@pytest.fixture
def here(tmp_path, monkeypatch):
    """This Ninaivu: a fresh 1.12 folder, as its launcher left it before the
    Control Panel opened (empty data/control and data/ai-models)."""
    path = _folder(tmp_path, "Ninaivu-1.12.0-linux-x64-portable")
    (path / "data" / "control").mkdir(parents=True)
    (path / "data" / "ai-models").mkdir()
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    monkeypatch.setenv("NINAIVU_PORTABLE_ROOT", str(path))
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(path / "data"))
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(path / "data" / "ai-models"))
    monkeypatch.setenv("NINAIVU_HOME", str(path / "data" / "control"))
    monkeypatch.setenv("NINAIVU_LOG_DIR", str(path / "logs"))
    return path


def _no_stop(_state):
    return None


def test_the_launcher_names_where_this_copy_keeps_each_thing(here):
    folder = portable.this_folder()
    assert folder.path == here
    assert folder.state == here / "data"
    assert folder.models == here / "data" / "ai-models"
    assert folder.control == here / "data" / "control" / ".ninaivu-control"
    assert not folder.has_library


def test_a_launcher_from_before_names_no_root_and_its_logs_say_where_it_is(monkeypatch, tmp_path):
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    monkeypatch.delenv("NINAIVU_PORTABLE_ROOT", raising=False)
    monkeypatch.setenv("NINAIVU_LOG_DIR", str(tmp_path / "Ninaivu" / "logs"))
    assert portable.root() == tmp_path / "Ninaivu"
    monkeypatch.delenv("NINAIVU_PORTABLE")
    assert portable.root() is None


def test_data_moved_in_by_hand_where_1_11_kept_it_is_put_beside_the_program(here):
    _index(here / "app" / "data" / "index.db", "moved by hand")
    (here / "app" / "ai-models" / "faces").mkdir(parents=True)
    (here / "app" / ".ninaivu-control").mkdir()
    (here / "app" / ".ninaivu-control" / "settings.json").write_text("{}")

    assert portable.settle()
    assert _words(here / "data" / "index.db") == "moved by hand"
    assert (here / "data" / "ai-models" / "faces").is_dir()
    assert (here / "data" / "control" / ".ninaivu-control" / "settings.json").is_file()
    assert not (here / "app" / "data").exists()
    assert portable.settle() == []                  # nothing left to move


def test_settle_leaves_a_library_already_in_place_alone(here):
    _index(here / "data" / "index.db", "this one")
    _index(here / "app" / "data" / "index.db", "a stray copy")
    assert portable.settle() == []
    assert _words(here / "data" / "index.db") == "this one"
    assert _words(here / "app" / "data" / "index.db") == "a stray copy"


def test_the_older_folder_beside_this_one_is_found(here, tmp_path):
    older = _folder(tmp_path, "Ninaivu-1.11.0-linux-x64-portable", layout="1.11",
                    version="1.11.0", words="the family")
    found = portable.older_beside(version="1.12.0")
    assert found is not None and found.path == older
    assert found.state == older / "app" / "data"
    assert found.version == "1.11.0"


def test_what_is_not_offered(here, tmp_path):
    _folder(tmp_path, "Ninaivu-2.0.0", version="2.0.0", words="opened by a newer one")
    _folder(tmp_path, "Ninaivu-empty")                          # no library
    (tmp_path / "Photos").mkdir()                               # not Ninaivu at all
    moved = _folder(tmp_path, "Ninaivu-1.10.10", version="1.10.10", words="moved already")
    (moved / portable.MOVED_RECORD).write_text(json.dumps({"to": str(here)}))
    assert portable.older_beside(version="1.12.0") is None


def test_the_most_recently_used_older_library_is_the_one_offered(here, tmp_path):
    import os
    first = _folder(tmp_path, "Ninaivu-1.10.10", version="1.10.10", words="old")
    second = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="newer")
    os.utime(first / "data" / "index.db", (1_000, 1_000))
    assert portable.older_beside(version="1.12.0").path == second


def test_nothing_is_offered_once_this_folder_has_a_library(here, tmp_path):
    _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="the family")
    _index(here / "data" / "index.db", "started fresh")
    assert portable.older_beside(version="1.12.0") is None


def test_the_library_is_brought_across_and_the_older_folder_says_where_it_went(here, tmp_path):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", layout="1.11", version="1.11.0",
                         words="the family")
    older = portable.older_beside(version="1.12.0")
    done = portable.bring_here(older, version="1.12.0", stop_older=_no_stop)

    assert _words(here / "data" / "index.db") == "the family"
    assert (here / "data" / "thumbs" / "2024" / "a.webp").read_bytes() == b"thumb"
    assert (here / "data" / "ai-models" / "clip" / "model.onnx").read_bytes() == b"weights"
    assert json.loads((here / "data" / "control" / ".ninaivu-control" / "settings.json")
                      .read_text()) == {"mode": "performance"}
    # Moved, not copied: nothing of the library is left in the older folder.
    assert not (older_path / "app" / "data").exists()
    assert not (older_path / "app" / "ai-models").exists()
    assert (older_path / "app" / "python").is_dir()             # its program stays
    note = (older_path / portable.MOVED_NOTE).read_text()
    assert str(here) in note and "Go back" in note
    assert json.loads((older_path / portable.MOVED_RECORD).read_text())["to"] == str(here)
    assert done["version"] == "1.11.0" and done["skipped"] == []
    assert portable.can_go_back() == "1.11.0"


def test_a_folder_on_another_drive_is_refused_with_nothing_moved(here, tmp_path, monkeypatch):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="the family")
    monkeypatch.setattr(portable, "_same_drive", lambda one, other: False)
    with pytest.raises(portable.PortableError, match="same drive"):
        portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                            stop_older=_no_stop)
    assert _words(older_path / "data" / "index.db") == "the family"
    assert not (here / "data" / "index.db").exists()


def test_a_library_opened_by_a_newer_version_is_refused(here, tmp_path):
    older = portable._layout(_folder(tmp_path, "Ninaivu-2.0.0", version="2.0.0", words="x"))
    with pytest.raises(portable.PortableError, match="newer"):
        portable.bring_here(older, version="1.12.0", stop_older=_no_stop)


def test_a_running_older_ninaivu_that_will_not_stop_keeps_its_library(here, tmp_path, monkeypatch):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="the family")

    def will_not_stop(_state):
        raise portable.PortableError("The older Ninaivu is still running")

    with pytest.raises(portable.PortableError, match="still running"):
        portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                            stop_older=will_not_stop)
    assert _words(older_path / "data" / "index.db") == "the family"


def test_going_back_before_the_newer_version_started_moves_everything_back(here, tmp_path):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", layout="1.11", version="1.11.0",
                         words="the family")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    done = portable.go_back(version="1.12.0", stop_this=_no_stop)

    assert done["index_put_back"] is None
    assert _words(older_path / "app" / "data" / "index.db") == "the family"
    assert (older_path / "app" / "ai-models" / "clip" / "model.onnx").is_file()
    assert (older_path / "app" / ".ninaivu-control" / "settings.json").is_file()
    assert not (older_path / portable.MOVED_NOTE).exists()
    assert not (here / "data" / "index.db").exists()
    assert portable.can_go_back() is None


def test_going_back_after_the_update_changed_the_index_puts_the_older_index_back(here, tmp_path):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="as 1.11 left it")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    state = here / "data"
    # The first start of the newer version: the copy, then its own changes.
    made = upgrade.prepare(state, version="1.12.0", environ={})["backup"]
    assert made is not None
    _index(state / "index.db", "changed by 1.12")

    done = portable.go_back(version="1.12.0", stop_this=_no_stop)
    assert done["index_put_back"] == str(made)
    assert _words(older_path / "data" / "index.db") == "as 1.11 left it"
    assert not (older_path / "data" / upgrade.VERSION_FILE).exists()
    assert (older_path / "data" / "thumbs" / "2024" / "a.webp").is_file()


def test_going_back_without_the_copy_moves_nothing(here, tmp_path):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="the family")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    (here / "data" / upgrade.VERSION_FILE).write_text(json.dumps({"version": "1.12.0"}))
    with pytest.raises(portable.PortableError, match="No copy"):
        portable.go_back(version="1.12.0", stop_this=_no_stop)
    assert _words(here / "data" / "index.db") == "the family"
    assert not (older_path / "data" / "index.db").exists()


def test_the_control_panel_offers_the_older_library_and_brings_it(here, tmp_path, monkeypatch):
    _folder(tmp_path, "Ninaivu-1.11.0", layout="1.11", version="1.11.0", words="the family")
    monkeypatch.setattr(portable, "_stop", _no_stop)
    asked, told = [], []
    brought = panel.offer_the_older_library(
        None, ask=lambda title, text: asked.append(text) or True,
        tell=lambda title, text: told.append(text), warn=lambda title, text: told.append(text))
    assert brought is True
    assert "Ninaivu 1.11.0" in asked[0] and "Go back" in asked[0]
    assert "Press Start" in told[0]
    assert _words(here / "data" / "index.db") == "the family"
    # And once it is here, nothing more is offered.
    assert panel.offer_the_older_library(None, ask=lambda *a: True) is None


def test_saying_no_in_the_control_panel_moves_nothing(here, tmp_path):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="the family")
    assert panel.offer_the_older_library(None, ask=lambda *a: False) is False
    assert _words(older_path / "data" / "index.db") == "the family"
    assert not (here / "data" / "index.db").exists()
