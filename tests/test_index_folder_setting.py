"""The index folder (state_dir) changed from Advanced settings.

On a one-computer Ninaivu and on the Primary, Ninaivu can be pointed at the
index already in another folder, from the next start: nothing is copied or
moved, the index used until then is kept as it is, and the administrator's
password is asked for. Never on the Secondary, nor on a copy following
another computer.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ninaivu.server import runfile
from ninaivu.storage import relocate

from conftest import ADMIN  # noqa: E402

PASSWORD = ADMIN[1]


def _index(folder: Path, photos: int, roots: list[str]) -> Path:
    folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder / "index.db")) as conn:
        conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, trashed INTEGER DEFAULT 0)")
        conn.executemany("INSERT INTO assets (trashed) VALUES (0)", [()] * photos)
        conn.commit()
    (folder / "config.json").write_text(json.dumps({"roots": roots}))
    (folder / "version.json").write_text(json.dumps({"version": "3.5.1"}))
    return folder.resolve()


@pytest.fixture()
def two_indexes(tmp_path):
    current = _index(tmp_path / "now", 3, ["/photos"])
    earlier = _index(tmp_path / "Toshiba" / "index", 7, ["/media/jaga/Toshiba/NinaivuMaster/Ninaivu Archive"])
    with patch.object(relocate, "_say"):
        yield current, earlier


def test_it_reads_the_given_folder_and_copies_nothing(two_indexes):
    current, earlier = two_indexes
    theirs = {p.name: p.read_bytes() for p in earlier.iterdir()}
    mine = {p.name: p.read_bytes() for p in current.iterdir()}
    record = relocate.request_switch(current, earlier)
    assert record["mode"] == "switch" and record["photographs"] == 7
    with runfile.starting_lock(current):
        assert relocate.apply_pending(current) == earlier
    assert relocate.resolve(current) == earlier
    # Neither index changed; nothing was copied either way.
    assert {p.name: p.read_bytes() for p in current.iterdir() if p.name in mine} == mine
    assert {p.name: p.read_bytes() for p in earlier.iterdir() if p.name in theirs} == theirs
    assert not (earlier / relocate.MANIFEST).exists()
    assert relocate.status(earlier)["last"]["switched"] is True
    from ninaivu.storage import index_check
    assert index_check.wanted(earlier)["reason"] == "switched"


def test_deleting_the_note_goes_back(two_indexes):
    current, earlier = two_indexes
    relocate.request_switch(current, earlier)
    with runfile.starting_lock(current):
        relocate.apply_pending(current)
    (current / relocate.REDIRECT).unlink()
    assert relocate.resolve(current) == current


@pytest.mark.parametrize("problem, says", [
    ("empty", "has no Ninaivu index"),
    ("same", "uses now"),
    ("newer", "newer than this"),
    ("moved", "moved or is waiting"),
    ("relative", "absolute folder path"),
])
def test_a_folder_it_cannot_read_is_refused(two_indexes, tmp_path, problem, says):
    current, earlier = two_indexes
    target = earlier
    if problem == "empty":
        target = tmp_path / "empty"
        target.mkdir()
    elif problem == "same":
        target = current
    elif problem == "newer":
        (earlier / "version.json").write_text(json.dumps({"version": "99.0"}))
    elif problem == "moved":
        relocate.write(earlier / relocate.REDIRECT, {"destination": str(tmp_path / "x")})
    elif problem == "relative":
        target = Path("Toshiba/index")
    with pytest.raises(ValueError, match=says):
        relocate.request_switch(current, target)
    assert not (current / relocate.PENDING).exists()


def test_an_index_in_use_is_refused(two_indexes):
    current, earlier = two_indexes
    with runfile.server_lock(earlier), pytest.raises(ValueError, match="Another Ninaivu"):
        relocate.request_switch(current, earlier)


def test_a_switch_that_cannot_happen_at_the_start_keeps_the_index(two_indexes):
    current, earlier = two_indexes
    relocate.request_switch(current, earlier)
    (earlier / "index.db").unlink()                     # the drive gone before the restart
    with runfile.starting_lock(current):
        assert relocate.apply_pending(current) is None
    assert relocate.resolve(current) == current
    assert relocate.status(current)["last"]["ok"] is False


@pytest.mark.parametrize("role, replica, editable", [
    ("single", None, True), ("primary", None, True), ("secondary", None, False),
    ("single", "http://primary.local:8080", False),
])
def test_only_a_one_computer_ninaivu_and_the_primary_may_change_it(role, replica, editable):
    with patch("ninaivu.mugil.role", return_value=role):
        assert relocate.index_folder_editable(SimpleNamespace(replica_from=replica)) is editable


# --- the console ----------------------------------------------------------------

@pytest.fixture()
def admin(as_admin):
    return as_admin


def test_advanced_settings_offers_it_and_never_saves_it(app, admin):
    settings = admin.get("/api/admin/settings/all").get_json()
    state_dir = next(s for g in settings["groups"] for s in g["settings"] if s["name"] == "state_dir")
    assert state_dir["action"] == "index_folder"
    assert admin.post("/api/admin/settings/all", json={"settings": {"state_dir": "/x"}}).status_code == 400


def test_the_console_shows_the_folder_then_asks_for_the_password(app, admin, tmp_path):
    cfg = app.config["MV_CONFIG"]
    earlier = _index(tmp_path / "earlier", 5, ["/photos"])
    url = "/api/admin/settings/index-folder"
    looked = admin.post(url, json={"folder": str(earlier), "check_only": True})
    assert looked.status_code == 200 and looked.get_json()["index"]["photographs"] == 5
    assert not (Path(cfg.state_dir) / relocate.PENDING).exists()

    unasked = admin.post(url, json={"folder": str(earlier)})
    assert unasked.status_code == 401 and unasked.get_json()["needs_password"]
    wrong = admin.post(url, json={"folder": str(earlier), "password": "not it at all"})
    assert wrong.status_code == 401 and "not right" in wrong.get_json()["error"]
    assert not (Path(cfg.state_dir) / relocate.PENDING).exists()

    from ninaivu.api import server_api
    with patch.object(server_api, "_restart_problem", return_value="Restart it yourself."):
        done = admin.post(url, json={"folder": str(earlier), "password": PASSWORD})
    assert done.status_code == 202, done.get_json()
    assert done.get_json()["pending"]["mode"] == "switch"
    assert json.loads((Path(cfg.state_dir) / relocate.PENDING).read_text())["destination"] == str(earlier)


def test_a_folder_without_an_index_is_refused_before_the_password(app, admin, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    answer = admin.post("/api/admin/settings/index-folder", json={"folder": str(empty), "check_only": True})
    assert answer.status_code == 400 and "no Ninaivu index" in answer.get_json()["error"]


def test_the_secondary_cannot_change_it(app, admin, tmp_path):
    earlier = _index(tmp_path / "earlier", 1, [])
    with patch.object(relocate, "index_folder_editable", return_value=False):
        answer = admin.post("/api/admin/settings/index-folder",
                            json={"folder": str(earlier), "password": PASSWORD})
    assert answer.status_code == 409
    assert not (Path(app.config["MV_CONFIG"].state_dir) / relocate.PENDING).exists()
