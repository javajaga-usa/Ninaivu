"""The family archive, after the review of 10 Oct 2026 (storage/keepsake.py).

A drive is somebody else's to write on, so links planted there are refused;
libraries keep folders of their own on it; a full drive stops the run; and
what goes in keeps to what the family may see.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.server import date_policy
from ninaivu.storage import db, keepsake
from ninaivu.storage.keepsake import FOLDER, RESERVE, SMALL_ROOM, THUMB_ROOM, Keepsake

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET visibility=2 WHERE rel_path LIKE 'private/%'")
    conn.commit()
    drive = tmp_path / "USB"
    drive.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    keeper = Keepsake(cfg, lambda: db.connect(cfg.db_path))
    return {"cfg": cfg, "conn": conn, "keeper": keeper, "drive": drive, "outside": outside}


def make(keeper, drive, **kw):
    keeper.start(str(drive), **kw)
    keeper.join(60)
    return keeper.status()


def data_of(base: Path) -> dict:
    text = (base / "data.js").read_text(encoding="utf-8")
    return json.loads(re.match(r"window\.NINAIVU_ARCHIVE = (.*);\s*$", text, re.S).group(1))


# -- 1. links planted on the drive are refused, never followed --------------------


@pytest.mark.parametrize("which", ["photos", "thumbs"])
def test_a_folder_of_the_archive_that_is_a_link_stops_the_run(lib, which):
    keep = lib["outside"] / "1.jpg"
    keep.write_bytes(b"somebody else's")
    base = lib["drive"] / FOLDER
    base.mkdir()
    (base / which).symlink_to(lib["outside"], target_is_directory=True)
    status = make(lib["keeper"], lib["drive"])
    assert "link" in status["error"], status
    assert [p.name for p in lib["outside"].iterdir()] == ["1.jpg"], "nothing copied there"
    assert keep.read_bytes() == b"somebody else's", "nothing removed or overwritten there"
    assert status["last_made"] == 0


def test_the_archive_folder_itself_as_a_link_stops_the_run(lib):
    (lib["drive"] / FOLDER).symlink_to(lib["outside"], target_is_directory=True)
    status = make(lib["keeper"], lib["drive"])
    assert "link" in status["error"], status
    assert not list(lib["outside"].iterdir())


def test_a_planted_part_file_is_replaced_not_written_through(lib):
    victim = lib["outside"] / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")
    base = lib["drive"] / FOLDER
    base.mkdir()
    for name in (".data.js.part", ".Open me.html.part", ".Read me first.html.part"):
        (base / name).symlink_to(victim)
    status = make(lib["keeper"], lib["drive"])
    assert not status["error"], status
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert (base / "data.js").is_file() and not (base / "data.js").is_symlink()
    assert not list(base.glob(".*.part"))


def test_a_link_inside_photos_is_not_followed(lib):
    base = lib["drive"] / FOLDER
    (base / "photos").mkdir(parents=True)
    (base / "photos" / "2023").symlink_to(lib["outside"], target_is_directory=True)
    status = make(lib["keeper"], lib["drive"])
    assert not list(lib["outside"].iterdir()), "nothing copied through the link"
    assert any(p["file"].startswith("2023/") for p in status["problems"]), status["problems"]


def test_a_planted_part_beside_a_photograph_is_not_written_through(lib):
    victim = lib["outside"] / "precious.txt"
    victim.write_text("keep me", encoding="utf-8")
    row = lib["conn"].execute("SELECT rel_path FROM assets WHERE rel_path LIKE 'shared/%' "
                              "ORDER BY id LIMIT 1").fetchone()
    target = lib["drive"] / FOLDER / "photos" / row["rel_path"]
    target.parent.mkdir(parents=True)
    target.with_name(f".{target.name}.part").symlink_to(victim)
    (lib["drive"] / FOLDER / "thumbs").mkdir()
    status = make(lib["keeper"], lib["drive"])
    assert not status["error"], status
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert target.is_file() and not target.is_symlink()


# -- 2 and 3. libraries keep folders of their own, whatever one run takes ---------


def test_two_libraries_of_one_name_get_folders_of_their_own(tmp_path):
    cfg = SimpleNamespace(roots=["/mnt/d1/Photos", "/mnt/d2/Photos", "/mnt/d3/photos", "/srv/Other"],
                          active_root="")
    keeper = Keepsake(cfg, lambda: None)
    folders = keeper.folders()
    assert folders == {"/mnt/d1/Photos": "Photos", "/mnt/d2/Photos": "Photos (2)",
                       "/mnt/d3/photos": "photos (3)", "/srv/Other": "Other"}
    one = {"root": "/mnt/d1/Photos", "rel_path": "2020/a.jpg", "kind": "picture"}
    two = {"root": "/mnt/d2/Photos", "rel_path": "2020/a.jpg", "kind": "picture"}
    assert Keepsake._target(tmp_path, one, False, folders) != Keepsake._target(tmp_path, two, False, folders)
    assert Keepsake._target(tmp_path, two, False, folders) == tmp_path / "photos" / "Photos (2)" / "2020" / "a.jpg"
    assert keeper.folders() == folders, "the same every time"


def test_the_layout_follows_the_libraries_set_up_not_the_run(lib, tmp_path):
    empty = tmp_path / "empty-library"
    empty.mkdir()
    lib["cfg"].roots = [lib["cfg"].active_root, str(empty)]
    status = make(lib["keeper"], lib["drive"])
    assert not status["error"], status
    name = Path(lib["cfg"].active_root).name
    items = data_of(lib["drive"] / FOLDER)["items"]
    assert items and all(i["f"].startswith(f"photos/{name}/") for i in items), items[:2]


# -- 4. a full drive stops the run, and a short run is not recorded as made -------


def test_a_drive_that_fills_stops_the_run_and_keeps_the_last_page(lib, monkeypatch):
    first = make(lib["keeper"], lib["drive"])
    assert not first["error"] and first["last_made"] > 0
    base = lib["drive"] / FOLDER
    before = (base / "data.js").read_text(encoding="utf-8")
    made = first["last_made"]
    shutil.rmtree(base / "photos" / "shared")              # so there is something to copy

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfileobj", full)
    monkeypatch.setattr(shutil, "copyfile", full)
    status = make(lib["keeper"], lib["drive"])
    assert "full" in status["error"], status
    assert status["last_made"] == made, "not recorded as made again"
    assert (base / "data.js").read_text(encoding="utf-8") == before
    assert not list(base.rglob("*.part"))


def test_a_run_with_problems_is_not_recorded_as_made(lib):
    row = lib["conn"].execute("SELECT root, rel_path FROM assets WHERE rel_path LIKE 'shared/%' "
                              "ORDER BY id LIMIT 1").fetchone()
    Path(row["root"], row["rel_path"]).unlink()
    status = make(lib["keeper"], lib["drive"])
    assert not status["error"] and status["problems"], status
    assert status["last_made"] == 0, "the Handover sheet would list it as done"
    assert "not counted as made" in status["message"]


def test_the_estimate_counts_thumbnails_and_smaller_copies(lib, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(RESERVE * 2, 0, RESERVE + 1))
    status = make(lib["keeper"], lib["drive"], small=True)
    assert "GB free" in status["error"], "no videos, but thumbnails still take room"
    item = {"id": 7, "root": "/r", "rel_path": "a.jpg", "kind": "picture", "size": 50_000_000, "mtime": 1}
    assert lib["keeper"]._room(lib["drive"], item, True, {}) == SMALL_ROOM + THUMB_ROOM
    assert lib["keeper"]._room(lib["drive"], item, False, {}) == 50_000_000 + THUMB_ROOM


# -- 5 and 6. what goes in ---------------------------------------------------------


def test_only_what_the_family_sees_keeps_to_their_date_limit(lib):
    conn = lib["conn"]
    assert lib["keeper"].plan(conn, False)
    db.set_meta(conn, date_policy.KEY, json.dumps(dict(date_policy.DEFAULTS, cutoff="2023-07-01",
                                                       family="before")))
    conn.commit()
    planned = lib["keeper"].plan(conn, False)
    assert planned and all(i["date_key"] and i["date_key"] < "2023-07-01" for i in planned), planned
    assert any(i["date_key"] >= "2023-07-01" for i in lib["keeper"].plan(conn, True)), \
        "everything, when asked for, is everything the administrator has"


def test_photographs_hidden_as_screenshots_or_papers_never_go(lib):
    conn = lib["conn"]
    ids = [r[0] for r in conn.execute("SELECT id FROM assets WHERE rel_path LIKE 'shared/%' ORDER BY id")]
    conn.execute("UPDATE assets SET visibility=2, vis_source='screen' WHERE id=?", (ids[0],))
    conn.execute("UPDATE assets SET visibility=2, vis_source='kind' WHERE id=?", (ids[1],))
    conn.execute("UPDATE assets SET visibility=2, vis_source='item' WHERE id=?", (ids[2],))
    conn.commit()
    planned = {i["id"] for i in lib["keeper"].plan(conn, True)}
    assert ids[0] not in planned and ids[1] not in planned
    assert ids[2] in planned, "hidden by hand goes in, when the hidden ones are asked for"


# -- 10. the letter's "Saved" ------------------------------------------------------


def test_saved_is_said_only_when_the_letter_was_saved():
    text = (ROOT / "ninaivu/static/js/keepsake.js").read_text(encoding="utf-8")
    act = text[text.index("async act(body)"):]
    assert "return false;" in act and "return true;" in act
    assert "if (await this.act({ letter: this.letter })) this.toast(i18n.t('Saved'))" in text



# -- quicker: little JPEGs in folders, a drive read once, images on threads --------


def test_little_jpegs_go_a_thousand_to_a_folder(lib):
    status = make(lib["keeper"], lib["drive"])
    assert not status["error"], status
    base = lib["drive"] / FOLDER
    items = data_of(base)["items"]
    shown = [i for i in items if i["t"]]
    assert shown, items[:2]
    for i in shown:
        assert i["t"] == f"thumbs/{i['id'] // keepsake.THUMBS_PER_FOLDER}/{i['id']}.jpg"
        assert (base / i["t"]).is_file()
    assert not [p for p in (base / "thumbs").iterdir() if p.is_file()], "none left in the top folder"


def test_a_drive_made_with_one_folder_of_little_jpegs_has_them_moved(lib):
    first = make(lib["keeper"], lib["drive"])
    assert not first["error"], first
    base = lib["drive"] / FOLDER
    thumbs = base / "thumbs"
    moved = {}
    for path in list(thumbs.rglob("*.jpg")):                # as a drive made before had them
        flat = thumbs / path.name
        path.rename(flat)
        moved[path.name] = flat.stat().st_ino
    for folder in [p for p in thumbs.iterdir() if p.is_dir()]:
        folder.rmdir()
    victim = lib["outside"] / "precious.jpg"
    victim.write_bytes(b"keep me")
    (thumbs / "999999.jpg").symlink_to(victim)
    again = make(lib["keeper"], lib["drive"])
    assert not again["error"] and again["copied"] == 0, again
    assert victim.read_bytes() == b"keep me" and not (thumbs / "999999.jpg").is_symlink()
    assert not [p for p in thumbs.iterdir() if not p.is_dir()], "the old ones are moved, the link removed"
    for i in data_of(base)["items"]:
        if i["t"]:
            name = Path(i["t"]).name
            assert (base / i["t"]).stat().st_ino == moved[name], "moved, not made again"


def test_made_again_reads_the_drive_once_and_copies_nothing(lib, monkeypatch):
    first = make(lib["keeper"], lib["drive"])
    assert not first["error"], first
    photos = str(lib["drive"] / FOLDER / "photos")
    looked: dict[str, int] = {}
    real = os.stat

    def counting(path, *a, **k):
        if str(path).startswith(photos):
            looked[str(path)] = looked.get(str(path), 0) + 1
        return real(path, *a, **k)

    def no_copy(*_a, **_k):
        raise AssertionError("nothing is copied again")

    monkeypatch.setattr(os, "stat", counting)
    monkeypatch.setattr(keepsake, "_copy", no_copy)
    monkeypatch.setattr(Keepsake, "_thumb", no_copy)
    again = make(lib["keeper"], lib["drive"])
    assert not again["error"] and again["copied"] == 0 and not again["problems"], again
    files = [p for p in looked if Path(p).is_file()]
    assert not files, f"the files are read with their folders, not one at a time: {files[:3]}"


def test_folders_once_checked_are_not_checked_again(tmp_path, monkeypatch):
    made: set[str] = set()
    keepsake._folders_in(tmp_path, tmp_path / "a" / "b", made)
    assert (tmp_path / "a" / "b").is_dir() and str(tmp_path / "a" / "b") in made

    def never(*_a):
        raise AssertionError("checked again")

    monkeypatch.setattr(keepsake, "_inside", never)
    keepsake._folders_in(tmp_path, tmp_path / "a" / "b", made)
    with pytest.raises(AssertionError):
        keepsake._folders_in(tmp_path, tmp_path / "a" / "c", made)


def test_images_are_made_on_threads_and_the_page_is_the_same(lib, tmp_path, monkeypatch):
    names: set[str] = set()
    real = Keepsake._thumb

    def thumb(self, *a):
        names.add(threading.current_thread().name)
        return real(self, *a)

    monkeypatch.setattr(Keepsake, "_thumb", thumb)
    monkeypatch.setattr(keepsake, "_image_threads", lambda: 1)
    one = make(lib["keeper"], lib["drive"], small=True)
    assert not one["error"], one
    other = tmp_path / "USB2"
    other.mkdir()
    monkeypatch.setattr(keepsake, "_image_threads", lambda: 4)
    four = make(lib["keeper"], other, small=True)
    assert not four["error"], four
    assert one["copied"] == four["copied"]
    assert data_of(lib["drive"] / FOLDER)["items"] == data_of(other / FOLDER)["items"], "in the same order"
    assert names and all(n.startswith("ninaivu-keepsake") and n != "ninaivu-keepsake" for n in names), names


def test_the_threads_follow_the_tuning():
    from ninaivu.utils import resources
    assert 1 <= keepsake._image_threads() <= resources.MAX_HELPERS


def test_a_drive_that_fills_while_making_little_jpegs_stops_the_run(lib, monkeypatch):
    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Keepsake, "_smaller", staticmethod(full))
    status = make(lib["keeper"], lib["drive"], small=True)
    assert "full" in status["error"], status
    assert status["last_made"] == 0
    assert not (lib["drive"] / FOLDER / "data.js").exists()
    assert not list((lib["drive"] / FOLDER).rglob("*.part"))


def test_stopped_while_making_little_jpegs(lib, monkeypatch):
    real = Keepsake._thumb

    def thumb(self, *a):
        self._stop.set()
        return real(self, *a)

    monkeypatch.setattr(Keepsake, "_thumb", thumb)
    status = make(lib["keeper"], lib["drive"])
    assert "Stopped" in status["message"], status
    assert not status["error"] and not list((lib["drive"] / FOLDER).rglob("*.part"))


def test_the_page_searches_words_worked_out_once_after_the_typing_pauses():
    script = keepsake._VIEWER[keepsake._VIEWER.index("<script>"):]
    draw = script[script.index("function draw()"):script.index("function open(")]
    assert "words(" not in draw and "i.s.indexOf(q)" in draw
    assert "data.items.forEach(function (i) { i.s = words(i); });" in script
    assert "setTimeout(draw, 150)" in script and "clearTimeout(typing)" in script
    assert "$('q').oninput = draw" not in script
