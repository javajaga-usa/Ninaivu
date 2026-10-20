"""The eighth audit's Highs (2026-10-20, D-01 to D-04) and the fold fixes
that make removing one of two nested library folders safe (D-06 to D-08)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import ADMIN, ids_of, login
from PIL import Image
from test_change_journal import FakeDrive, keeping  # noqa: F401
from test_nested_library_folders import face, nested, person, photo, row  # noqa: F401

from ninaivu.media.scanner import Scanner
from ninaivu.storage import db, journal, stories
from ninaivu.storage import films as film_store


def _remove(n, which, **query):
    r = n["console"].delete("/api/admin/libraries",
                            query_string={"path": n[which], **query})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# -- D-01: the bin's clean-up keeps the recording of a photograph put back ----

def test_bin_cleanup_keeps_the_story_of_a_photo_dragged_back(app, people):
    conn = people["conn"]
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    target = ids_of(admin)[0]
    folder = stories._folder_of(conn)                       # noqa: SLF001
    folder.mkdir(exist_ok=True)
    story = stories.add(conn, folder, target, b"ID3" + b"\0" * 2000, mime="audio/mpeg",
                        created_by=None, speaker="Paati", text="hello")
    r = admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    assert r.status_code == 200, r.get_json()
    entry = dict(conn.execute("SELECT * FROM recycled WHERE restored_at IS NULL").fetchone())
    # Dragged back out of _deleted in Finder: the obvious undo.
    shutil.move(entry["bin_path"], Path(entry["root"]) / entry["rel_path"])

    assert admin.get("/api/recycle").status_code == 200

    kept = folder / "_orphaned" / story["file"]
    assert kept.exists() or (folder / story["file"]).exists()


# -- D-02: removing a folder whose only work is privacy asks first ------------

def test_remove_counts_hidden_and_private_photos(nested):  # noqa: F811
    n, conn, cfg = nested, nested["conn"], nested["cfg"]
    cfg.roots.remove(n["archive"])
    conn.execute("DELETE FROM assets WHERE root=?", (n["archive"],))
    conn.commit()
    p0 = row(conn, n["master"], "Ninaivu Archive/2024/01/02/p0.jpg")["id"]
    p1 = row(conn, n["master"], "Ninaivu Archive/2024/01/02/p1.jpg")["id"]
    db.set_visibility(conn, [p0], 2, source="manual")
    db.set_private(conn, [p1], n["maya"].id)

    r = n["console"].delete("/api/admin/libraries", query_string={"path": n["master"]})
    assert r.status_code == 409
    assert r.get_json()["losing"] == 2
    assert "privacy" in r.get_json()["error"]

    _remove(n, "master", force=1)
    copies = Path(cfg.state_dir) / "backups" / "before-remove"
    assert copies.is_dir() and any(copies.iterdir())


# -- D-03: a fold carries the folder rules ------------------------------------

def _scan_all(cfg):
    scanner = Scanner(cfg)
    for root in cfg.roots:
        scanner._run(Path(root), full=True)                 # noqa: SLF001
    return scanner


def test_fold_of_the_inner_folder_carries_its_rules(nested):  # noqa: F811
    n, conn = nested, nested["conn"]
    private = Path(n["archive"]) / "Private"
    photo(private / "a.jpg", 11)
    scanner = _scan_all(n["cfg"])
    db.set_folder_visibility(conn, n["archive"], "Private", 2, None)

    _remove(n, "archive", force=1)

    rules = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT root, folder, visibility FROM folder_rules")}
    assert rules.get((n["master"], "Ninaivu Archive/Private")) == 2
    assert (n["archive"], "Private") not in rules
    photo(private / "b.jpg", 77)                            # a new one, later
    scanner._run(Path(n["master"]), full=False)             # noqa: SLF001
    assert row(conn, n["master"], "Ninaivu Archive/Private/b.jpg")["visibility"] == 2


def test_fold_of_the_outer_folder_carries_its_rules(nested):  # noqa: F811
    n, conn = nested, nested["conn"]
    private = Path(n["archive"]) / "Private"
    photo(private / "a.jpg", 11)
    scanner = _scan_all(n["cfg"])
    db.set_folder_visibility(conn, n["master"], "Ninaivu Archive/Private", 2, None)
    db.set_folder_visibility(conn, n["master"], "Elsewhere", 2, None)

    _remove(n, "master", force=1)

    rules = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT root, folder, visibility FROM folder_rules")}
    assert rules.get((n["archive"], "Private")) == 2
    assert (n["archive"], "Elsewhere") not in rules
    photo(private / "b.jpg", 77)
    scanner._run(Path(n["archive"]), full=False)            # noqa: SLF001
    assert row(conn, n["archive"], "Private/b.jpg")["visibility"] == 2


# -- D-06: a confirmed name wins over the twin's guess ------------------------

def test_fold_keeps_a_confirmed_name_over_a_guess(nested):  # noqa: F811
    n, conn = nested, nested["conn"]
    going = row(conn, n["archive"], "2024/01/02/p0.jpg")["id"]
    twin = row(conn, n["master"], "Ninaivu Archive/2024/01/02/p0.jpg")["id"]
    paati, mama = person(conn, "Paati"), person(conn, "Mama")
    face(conn, going, paati)
    conn.execute("INSERT INTO faces(asset_id, person_id, source, confidence, bbox, embedding) "
                 "VALUES (?, ?, 'auto', 0.6, ?, ?)",
                 (twin, mama, json.dumps([10, 10, 50, 50]), b"\0" * 8))
    conn.commit()

    _remove(n, "archive", force=1)

    named = conn.execute("SELECT person_id, source FROM faces WHERE asset_id=?",
                         (twin,)).fetchall()
    assert (paati, "confirmed") in [tuple(r) for r in named]


# -- D-07: films and books follow the fold -----------------------------------

def test_fold_carries_films(nested):  # noqa: F811
    n, conn = nested, nested["conn"]
    going = row(conn, n["archive"], "2024/01/02/p0.jpg")["id"]
    twin = row(conn, n["master"], "Ninaivu Archive/2024/01/02/p0.jpg")["id"]
    film = film_store.create(conn, owner_id=n["admin"].id, title="t", subtitle="",
                             source_kind="", source_id=None, size="720p",
                             subtitles="none", music_id=None,
                             item_ids=[going, twin], story_ids=[])

    _remove(n, "archive", force=1)

    items = json.loads(conn.execute("SELECT item_ids FROM memory_films WHERE id=?",
                                    (film,)).fetchone()[0])
    assert items == [twin]


# -- D-08: a fold takes a copy of the index first ------------------------------

def test_fold_takes_a_copy_of_the_index(nested):  # noqa: F811
    n = nested
    _remove(n, "archive", force=1)
    copies = Path(n["cfg"].state_dir) / "backups" / "before-remove"
    assert copies.is_dir() and any(copies.iterdir())


# -- D-04: the drive's change mark waits for the indexing ---------------------

def test_change_mark_is_kept_only_once_the_files_are_indexed(keeping, monkeypatch):  # noqa: F811
    cfg, conn = keeping
    root = cfg.active_root
    drive = FakeDrive(now=900)
    monkeypatch.setattr(journal, "_fsevents", lambda: drive)
    for i in range(3):
        Image.new("RGB", (320, 200), (i, 9, 9)).save(Path(root) / f"misc/import-{i}.jpg")
    scanner = Scanner(cfg)
    monkeypatch.setattr(scanner, "_index", lambda *a, **k: scanner._stop.set())  # noqa: SLF001

    scanner._run(Path(root), full=False)                    # noqa: SLF001

    assert not journal.stored_mark(conn, root) or \
        journal.stored_mark(conn, root).get("event") != 900
