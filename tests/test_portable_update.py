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


def test_the_same_version_with_an_index_of_another_shape_puts_the_older_index_back(
        here, tmp_path, monkeypatch):
    """A build made before its version is raised (4.0 while it still says
    3.7.0) changes the index's shape under the same number. Handed back as
    it was, the older version refused it (schema 7): Go back puts back the
    copy that build's first start took."""
    older_path = _folder(tmp_path, "Ninaivu-1.12.0", version="1.12.0", words="as it was")
    (older_path / "data" / upgrade.VERSION_FILE).write_text(
        json.dumps({"version": "1.12.0", "shape": "the older shape"}))
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    state = here / "data"
    monkeypatch.setattr(upgrade, "index_shape", lambda: "the newer shape")
    made = upgrade.prepare(state, version="1.12.0", environ={})["backup"]
    assert made is not None
    _index(state / "index.db", "changed by the newer build")

    done = portable.go_back(version="1.12.0", stop_this=_no_stop)
    assert done["index_put_back"] == made
    assert _words(older_path / "data" / "index.db") == "as it was"


def test_the_same_version_that_changed_nothing_hands_the_index_back_as_it_is(here, tmp_path):
    """A copy the older folder made at an update of its own, before the
    library came here, is never put back over what was done since."""
    older_path = _folder(tmp_path, "Ninaivu-1.12.0", version="1.12.0", words="as it was")
    (older_path / "data" / upgrade.VERSION_FILE).write_text(json.dumps(
        {"version": "1.12.0", "shape": upgrade.index_shape(), "since": 1_000.0,
         "backup": str(older_path / "data" / "backups" / "before-update" / "old.tar.gz")}))
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    state = here / "data"
    assert upgrade.prepare(state, version="1.12.0", environ={})["backup"] is None
    _index(state / "index.db", "a caption added since")

    done = portable.go_back(version="1.12.0", stop_this=_no_stop)
    assert done["index_put_back"] is None
    assert _words(older_path / "data" / "index.db") == "a caption added since"


def test_going_back_without_the_copy_moves_nothing(here, tmp_path):
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="the family")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    (here / "data" / upgrade.VERSION_FILE).write_text(json.dumps({"version": "1.12.0"}))
    with pytest.raises(portable.PortableError, match="No copy"):
        portable.go_back(version="1.12.0", stop_this=_no_stop)
    assert _words(here / "data" / "index.db") == "the family"
    assert not (older_path / "data" / "index.db").exists()


def test_going_back_keeps_a_copy_of_what_the_newer_version_did(here, tmp_path):
    """Audit C-04: the newer version's work (albums, names, capsules) was
    replaced with no copy kept."""
    from ninaivu.storage import backup
    import tarfile
    _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="as 1.11 left it")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    state = here / "data"
    upgrade.prepare(state, version="1.12.0", environ={})
    _index(state / "index.db", "months of 1.12")

    done = portable.go_back(version="1.12.0", stop_this=_no_stop)
    kept = Path(done["kept"])
    assert kept.is_file() and portable.BEFORE_GO_BACK.name in kept.parts
    backup.verify_bundle(kept)
    with tarfile.open(kept) as tar, tempfile_dir() as out:
        tar.extract("state/index.db", out, filter="data")
        assert _words(Path(out) / "state" / "index.db") == "months of 1.12"


def test_a_go_back_cut_short_can_be_pressed_again(here, tmp_path, monkeypatch):
    """Audit C-04: a failed move left the library split, the record gone and
    nothing offered."""
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", layout="1.11", version="1.11.0",
                         words="the family")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    real = portable._move
    calls = {"n": 0}

    def flaky(source, target, skipped):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("held open")
        return real(source, target, skipped)

    monkeypatch.setattr(portable, "_move", flaky)
    with pytest.raises(portable.PortableError, match="press Go back again"):
        portable.go_back(version="1.12.0", stop_this=_no_stop)
    assert portable.can_go_back() == "1.11.0", "still offered"
    monkeypatch.setattr(portable, "_move", real)
    portable.go_back(version="1.12.0", stop_this=_no_stop)
    assert _words(older_path / "app" / "data" / "index.db") == "the family"
    assert (older_path / "app" / "ai-models" / "clip" / "model.onnx").is_file()
    assert (older_path / "app" / ".ninaivu-control" / "settings.json").is_file()
    assert portable.can_go_back() is None
    assert not (here / portable.GOING_BACK).exists()
    assert not (older_path / "app" / "data" / portable.BROUGHT_RECORD).exists()


def test_a_bring_across_cut_short_puts_everything_back(here, tmp_path, monkeypatch):
    """Audit C-04: the library moved, the models did not, and "try again"
    was refused because this folder already had a library."""
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", layout="1.11", version="1.11.0",
                         words="the family")
    real = portable._move
    calls = {"n": 0}

    def flaky(source, target, skipped):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("held open")
        return real(source, target, skipped)

    monkeypatch.setattr(portable, "_move", flaky)
    with pytest.raises(portable.PortableError, match="Everything is back"):
        portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                            stop_older=_no_stop)
    assert _words(older_path / "app" / "data" / "index.db") == "the family"
    assert not (here / "data" / "index.db").exists()
    monkeypatch.setattr(portable, "_move", real)
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    assert _words(here / "data" / "index.db") == "the family"


def tempfile_dir():
    import tempfile
    return tempfile.TemporaryDirectory()


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


def _panel_for(here: Path):
    from types import SimpleNamespace
    from ninaivu.desktop.control import Controller
    return Controller(root=here, cfg=SimpleNamespace(state_dir=here / "data", host="127.0.0.1",
                                                     port=5000, admin_port=3000, ai_engine="off"))


def test_an_update_that_never_starts_goes_back_by_itself_on_the_second_try(
        here, tmp_path, monkeypatch):
    """3.0: a newer portable version that cannot start on this computer gives
    the library back to the version it came from, with no Go back pressed."""
    import ninaivu
    monkeypatch.setattr(ninaivu, "__version__", "1.12.0")
    opened = []
    monkeypatch.setattr(portable, "launch", lambda path: opened.append(path) or True)
    older_path = _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="as 1.11 left it")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    state = here / "data"
    assert upgrade.prepare(state, version="1.12.0", environ={})["backup"]
    assert not upgrade.has_served(state, "1.12.0")
    _index(state / "index.db", "changed by 1.12")
    panel_ = _panel_for(here)

    assert panel_.failed_start() is None                 # the first failure is only counted
    assert (state / "index.db").is_file()
    said = panel_.failed_start()
    assert said and "went back to Ninaivu 1.11.0 by itself" in said
    assert panel_.went_back
    assert opened == [older_path]
    assert _words(older_path / "data" / "index.db") == "as 1.11 left it"
    assert not (state / "index.db").exists()


def test_an_update_that_has_served_once_never_goes_back_by_itself(here, tmp_path, monkeypatch):
    import ninaivu
    monkeypatch.setattr(ninaivu, "__version__", "1.12.0")
    monkeypatch.setattr(portable, "launch", lambda path: pytest.fail("must not open"))
    _folder(tmp_path, "Ninaivu-1.11.0", version="1.11.0", words="the family")
    portable.bring_here(portable.older_beside(version="1.12.0"), version="1.12.0",
                        stop_older=_no_stop)
    state = here / "data"
    upgrade.prepare(state, version="1.12.0", environ={})
    upgrade.note_good_start(state)
    assert upgrade.has_served(state, "1.12.0")
    panel_ = _panel_for(here)
    for _ in range(3):
        assert panel_.failed_start() is None
    assert _words(state / "index.db") == "the family"
    assert not panel_.went_back


def test_a_record_from_before_counts_as_having_served(tmp_path):
    (tmp_path / upgrade.VERSION_FILE).write_text(json.dumps({"version": "2.2.0"}))
    assert upgrade.has_served(tmp_path, "2.2.0")
    assert not upgrade.has_served(tmp_path, "3.0.0")
    upgrade.note_good_start(tmp_path)                    # leaves an old record alone
    assert "good_at" not in json.loads((tmp_path / upgrade.VERSION_FILE).read_text())


def _relocate(state, target):
    from ninaivu.storage import relocate
    relocate.request(state, target)
    assert relocate.apply_pending(state) == target
    return target


def test_upgrade_uses_live_relocated_index_and_keeps_its_location(here, tmp_path):
    older_path = _folder(tmp_path, 'Ninaivu-old', version='1.11.0', words='recovery copy')
    external = _relocate(older_path / 'data', tmp_path / 'chosen data')
    _index(external / 'index.db', 'current family library')
    older = portable.older_beside(version='1.12.0')
    assert older.state == external
    stopped = []
    portable.bring_here(older, version='1.12.0', stop_older=stopped.append)
    assert stopped == [external]
    assert portable.this_folder().state == external
    assert _words(external / 'index.db') == 'current family library'
    assert portable.this_folder().models == external / 'ai-models'
    assert portable.can_go_back() == '1.11.0'


@pytest.mark.parametrize('when', ['before-upgrade', 'after-upgrade'])
def test_go_back_after_relocation_restores_older_index_at_chosen_location(here, tmp_path, when):
    from ninaivu.server.config import Config
    older_path = _folder(tmp_path, 'Ninaivu-old', version='1.11.0', words='older version')
    external = tmp_path / 'chosen data'
    if when == 'before-upgrade':
        _relocate(older_path / 'data', external)
    portable.bring_here(portable.older_beside(version='1.12.0'), version='1.12.0',
                        stop_older=_no_stop)
    state = portable.this_folder().state
    assert upgrade.prepare(state, version='1.12.0', environ={})['backup']
    _index(state / 'index.db', 'newer version changes')
    if when == 'after-upgrade':
        _relocate(state, external)
    done = portable.go_back(version='1.12.0', stop_this=_no_stop)
    assert _words(external / 'index.db') == 'older version'
    assert Path(done['kept']).is_file()
    assert portable._layout(older_path).state == external
    assert not (older_path / portable.MOVED_RECORD).exists()
    assert portable.this_folder().moved_to == older_path
    assert Config.load().state_dir == external
    assert portable.can_go_back() is None


def test_unavailable_relocated_drive_is_not_replaced_with_recovery_copy(here, tmp_path):
    older_path = _folder(tmp_path, 'Ninaivu-old', version='1.11.0', words='recovery')
    external = _relocate(older_path / 'data', tmp_path / 'disconnected')
    (external / 'index.db').unlink()
    with pytest.raises(portable.PortableError, match='unavailable'):
        portable.older_beside(version='1.12.0')
    assert not (here / 'data' / 'index.db').exists()


def test_rolled_back_program_cannot_reopen_shared_index_until_reactivated(here, tmp_path):
    older_path = _folder(tmp_path, 'Ninaivu-old', version='1.11.0', words='family')
    external = _relocate(older_path / 'data', tmp_path / 'chosen data')
    portable.bring_here(portable.older_beside(version='1.12.0'), version='1.12.0',
                        stop_older=_no_stop)
    portable.go_back(version='1.12.0', stop_this=_no_stop)
    with pytest.raises(portable.PortableError, match='inactive'):
        portable.require_active()
    portable.bring_here(portable.older_beside(version='1.12.0'), version='1.12.0',
                        stop_older=_no_stop)
    portable.require_active()
    assert portable.this_folder().state == external
    assert _words(external / 'index.db') == 'family'


def test_live_environment_keeps_models_and_panel_settings_with_the_index(here, tmp_path, monkeypatch):
    from ninaivu.media import model_catalog
    state = here / 'data'
    _index(state / 'index.db', 'family')
    external = _relocate(state, tmp_path / 'chosen data')
    portable.activate_environment()
    assert portable.this_folder().state == external
    assert Path(portable.os.environ['NINAIVU_HOME']) == external / 'control'
    assert Path(portable.os.environ['NINAIVU_AI_MODELS_DIR']) == external / 'ai-models'
    monkeypatch.setattr(model_catalog, '_root', None)
    assert model_catalog.models_root() == external / 'ai-models'


def test_live_environment_keeps_explicit_external_model_directory(here, tmp_path, monkeypatch):
    state = here / 'data'
    _index(state / 'index.db', 'family')
    external_models = tmp_path / 'separate models'
    monkeypatch.setenv('NINAIVU_AI_MODELS_DIR', str(external_models))
    _relocate(state, tmp_path / 'chosen data')
    portable.activate_environment()
    assert portable.os.environ['NINAIVU_AI_MODELS_DIR'] == str(external_models)


def test_shared_home_index_can_be_taken_over_and_returned_between_programs(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    old = _folder(tmp_path, 'old program')
    new = _folder(tmp_path, 'new program')
    home_state = tmp_path / 'home/.ninaivu'
    _index(home_state / 'index.db', 'family')
    (home_state / upgrade.VERSION_FILE).write_text(json.dumps({'version': '1.11.0'}))
    portable.note_active(home_state, old)
    assert portable._layout(old).state == home_state
    assert portable.older_beside(portable._layout(new), version='1.12.0').path == old
    portable.bring_here(portable._layout(old), portable._layout(new), version='1.12.0',
                        stop_older=_no_stop)
    assert portable._owner(home_state) == new
    portable.go_back(portable._layout(new), version='1.12.0', stop_this=_no_stop)
    assert portable._owner(home_state) == old
    assert _words(home_state / 'index.db') == 'family'


def test_relative_portable_path_is_saved_as_an_absolute_location(tmp_path, monkeypatch):
    old = _folder(tmp_path, 'relative old', version='1.11.0', words='family')
    monkeypatch.chdir(tmp_path)
    assert portable._layout(Path('relative old')).path == old


def test_home_index_ownership_survives_relocation_recovery_cleanup(tmp_path, monkeypatch):
    from ninaivu.storage import recovery_cleanup
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    old = _folder(tmp_path, 'older program')
    new = _folder(tmp_path, 'newer program')
    state = tmp_path / 'home/.ninaivu'
    _index(state / 'index.db', 'family')
    (state / upgrade.VERSION_FILE).write_text(json.dumps({'version': '1.11.0'}))
    portable.note_active(state, old)
    live = _relocate(state, tmp_path / 'chosen data')
    recovery_cleanup.clean(live)
    assert not (state / portable.OWNER).exists()
    assert portable._layout(old).state == live
    portable.bring_here(portable._layout(old), portable._layout(new), version='1.12.0',
                        stop_older=_no_stop)
    assert portable._owner(state) == new
    assert portable._layout(new).state == live


@pytest.mark.parametrize('operation', ['bring', 'go-back'])
def test_portable_handover_holds_both_starting_gates(here, tmp_path, monkeypatch, operation):
    from ninaivu.server import runfile
    old_path = _folder(tmp_path, 'older program', layout='1.11', version='1.11.0', words='family')
    old = portable._layout(old_path)
    if operation == 'go-back':
        portable.bring_here(old, version='1.12.0', stop_older=_no_stop)
    source = old.state if operation == 'bring' else here / 'data'
    target = here / 'data' if operation == 'bring' else old.state
    real_move = portable._move
    checked = []
    def checked_move(src, dst, skipped):
        for state in (source, target):
            with pytest.raises(runfile.AlreadyStarting), runfile.starting_lock(state):
                pytest.fail('A competing server acquired the state folder during handover')
        checked.append(src)
        return real_move(src, dst, skipped)
    monkeypatch.setattr(portable, '_move', checked_move)
    if operation == 'bring':
        portable.bring_here(old, version='1.12.0', stop_older=_no_stop)
    else:
        portable.go_back(version='1.12.0', stop_this=_no_stop)
    assert checked
