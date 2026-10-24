"""Content fingerprints (2.0 proposal B, first stage): every file known by its
bytes as well as by its place, so a file moved or renamed keeps its row, and
exact copies are found across library folders. Opt-in with the
``content_fingerprints`` setting; off, scans are as in 1.12."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, fingerprints


def _rescan(cfg, full: bool = False) -> Scanner:
    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=full)
    return scanner


def _row(conn, rel: str):
    return conn.execute("SELECT * FROM assets WHERE rel_path=?", (rel,)).fetchone()


def _album_with(conn, asset_id: int) -> int:
    album = db.create_album(conn, "Kept")
    with db._write_lock:
        conn.execute("INSERT INTO album_items(album_id, asset_id, added_at) VALUES(?,?,?)",
                     (album, asset_id, time.time()))
        conn.commit()
    return album


def test_off_by_default_and_on_the_advanced_page():
    assert Config().content_fingerprints is False
    assert "content_fingerprints" in GROUPS["Advanced"]


def test_off_a_scan_takes_no_fingerprints(scanned):
    cfg, conn, _ = scanned
    assert conn.execute("SELECT COUNT(*) FROM content_fingerprints").fetchone()[0] == 0


def test_a_file_is_fingerprinted_by_its_bytes(tmp_path: Path):
    one = tmp_path / "a.bin"
    one.write_bytes(b"ninaivu" * 1000)
    two = tmp_path / "b.bin"
    two.write_bytes(b"ninaivu" * 1000)
    digest, size, mtime = fingerprints.fingerprint_file(one)
    assert len(digest) == 64 and size == 7000 and mtime == os.stat(one).st_mtime
    assert fingerprints.fingerprint_file(two)[0] == digest
    two.write_bytes(b"ninaivu" * 999 + b"ninaivU")
    assert fingerprints.fingerprint_file(two)[0] != digest
    if fingerprints.ALGO == "blake3":
        import blake3
        assert digest == blake3.blake3(b"ninaivu" * 1000).hexdigest()
    with pytest.raises(FileNotFoundError):
        fingerprints.fingerprint_file(tmp_path / "gone.bin")
    assert fingerprints.fingerprint_file(one, stop=lambda: True) is None


def test_on_every_file_gets_one_and_a_rescan_reads_none_again(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    _rescan(cfg)
    files = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0").fetchone()[0]
    assert files > 0
    assert conn.execute("SELECT COUNT(*) FROM content_fingerprints WHERE algo=?",
                        (fingerprints.ALGO,)).fetchone()[0] == files
    assert scanner_mod.owed(conn, cfg.active_root, "fingerprints").startswith("0:")

    read: list[str] = []
    real = fingerprints.fingerprint_file
    monkeypatch.setattr(fingerprints, "fingerprint_file",
                        lambda path, stop=None: read.append(str(path)) or real(path, stop))
    _rescan(cfg)
    assert read == []


def test_an_edited_file_owes_its_fingerprint_again(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    _rescan(cfg)
    rel = "misc/plain.png"
    before = conn.execute("SELECT f.digest FROM content_fingerprints f JOIN assets a "
                          "ON a.id=f.asset_id WHERE a.rel_path=?", (rel,)).fetchone()[0]
    path = Path(cfg.active_root) / rel
    Image.new("RGB", (210, 200), (200, 10, 10)).save(path)
    os.utime(path, (time.time() + 5, time.time() + 5))
    _rescan(cfg)
    after = conn.execute("SELECT f.digest FROM content_fingerprints f JOIN assets a "
                         "ON a.id=f.asset_id WHERE a.rel_path=?", (rel,)).fetchone()[0]
    assert after != before
    assert scanner_mod.owed(conn, cfg.active_root, "fingerprints").startswith("0:")


def test_a_moved_file_keeps_its_row_and_what_was_done_with_it(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    _rescan(cfg)
    old = _row(conn, "shared/holiday/beach1.jpg")
    album = _album_with(conn, old["id"])
    with db._write_lock:
        conn.execute("UPDATE assets SET caption='Beach day', caption_source='manual' "
                     "WHERE id=?", (old["id"],))
        conn.commit()

    root = Path(cfg.active_root)
    (root / "trips/2024").mkdir(parents=True)
    shutil.move(root / "shared/holiday/beach1.jpg", root / "trips/2024/renamed.jpg")
    _rescan(cfg)

    assert _row(conn, "shared/holiday/beach1.jpg") is None
    new = _row(conn, "trips/2024/renamed.jpg")
    assert new["id"] == old["id"]
    assert new["filename"] == "renamed.jpg" and new["folder"] == "trips/2024"
    assert new["caption"] == "Beach day"
    # Its thumbnails take the new path's name (audit C-39), so a file that
    # later arrives at the old path does not draw over them.
    from ninaivu.media import media
    assert new["thumb"] == media.thumb_base(cfg.active_root, "trips/2024/renamed.jpg")
    assert list(Path(cfg.thumbs_dir).glob(new["thumb"] + "_*"))
    assert not list(Path(cfg.thumbs_dir).glob(old["thumb"] + "_*"))
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=? AND asset_id=?",
                        (album, old["id"])).fetchone()[0] == 1


def test_a_renamed_folder_keeps_every_row(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    _rescan(cfg)
    before = {r["filename"]: r["id"] for r in conn.execute(
        "SELECT id, filename FROM assets WHERE folder='shared/holiday'")}
    root = Path(cfg.active_root)
    (root / "shared/holiday").rename(root / "shared/goa 2019")
    _rescan(cfg)
    after = {r["filename"]: r["id"] for r in conn.execute(
        "SELECT id, filename FROM assets WHERE folder='shared/goa 2019'")}
    assert after == before and len(after) == 4


def test_a_move_to_another_day_takes_that_day_when_the_date_came_from_the_folder(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    root = Path(cfg.active_root)
    day = root / "2019/03/04"
    day.mkdir(parents=True)
    Image.new("RGB", (240, 200), (1, 2, 3)).save(day / "scan.jpg")
    os.utime(day / "scan.jpg", (time.time(), time.time()))
    _rescan(cfg)
    old = _row(conn, "2019/03/04/scan.jpg")
    assert old["date_source"] == "path" and old["date_key"] == "2019-03-04"

    (root / "2019/03/09").mkdir(parents=True)
    shutil.move(day / "scan.jpg", root / "2019/03/09/scan.jpg")
    _rescan(cfg)
    new = _row(conn, "2019/03/09/scan.jpg")
    assert new["id"] == old["id"]
    assert new["date_key"] == "2019-03-09"


def test_off_a_moved_file_is_a_new_row_as_in_1_12(scanned):
    cfg, conn, _ = scanned
    old = _row(conn, "shared/holiday/beach2.jpg")
    album = _album_with(conn, old["id"])
    root = Path(cfg.active_root)
    shutil.move(root / "shared/holiday/beach2.jpg", root / "misc/beach2.jpg")
    _rescan(cfg)
    # Not compared by id: SQLite can hand a deleted row's id to the next one.
    assert _row(conn, "misc/beach2.jpg") is not None
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?",
                        (album,)).fetchone()[0] == 0


def test_two_identical_copies_moved_together_keep_one_row_each(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    _rescan(cfg)
    rows = {r["rel_path"]: r["id"] for r in conn.execute(
        "SELECT id, rel_path FROM assets WHERE folder='2023/05/12'")}
    assert {"2023/05/12/shot0.jpg", "2023/05/12/shot0_copy.jpg"} <= set(rows)
    root = Path(cfg.active_root)
    (root / "2023/05/12").rename(root / "2023/05/12 birthday")
    _rescan(cfg)
    after = {r["filename"]: r["id"] for r in conn.execute(
        "SELECT id, filename FROM assets WHERE folder='2023/05/12 birthday'")}
    # Each copy keeps its own row, not its twin's (audit C-40).
    assert after["shot0.jpg"] == rows["2023/05/12/shot0.jpg"]
    assert after["shot0_copy.jpg"] == rows["2023/05/12/shot0_copy.jpg"]
    assert sorted(after.values()) == sorted(rows.values())


def test_a_file_with_no_fingerprint_yet_moves_as_before(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    album = _album_with(conn, _row(conn, "misc/plain.png")["id"])
    root = Path(cfg.active_root)
    shutil.move(root / "misc/plain.png", root / "misc/plain-moved.png")
    _rescan(cfg)                     # nothing was fingerprinted before the move
    assert _row(conn, "misc/plain-moved.png") is not None
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?",
                        (album,)).fetchone()[0] == 0


def test_new_files_of_another_size_are_not_read_to_match_moves(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    _rescan(cfg)
    root = Path(cfg.active_root)
    (root / "misc/plain.png").unlink()
    Image.new("RGB", (900, 700), (5, 5, 5)).save(root / "misc/new.jpg")
    read: list[str] = []
    real = fingerprints.fingerprint_file
    monkeypatch.setattr(fingerprints, "fingerprint_file",
                        lambda path, stop=None: read.append(Path(path).name) or real(path, stop))
    pairs = fingerprints.match_moves(conn, cfg.active_root, ["misc/plain.png"],
                                     [("misc/new.jpg", os.stat(root / "misc/new.jpg"))])
    assert pairs == [] and read == []


def test_exact_copies_are_grouped_across_library_folders(scanned, tmp_path: Path):
    cfg, conn, _ = scanned
    other = tmp_path / "second-drive"
    other.mkdir()
    shutil.copy(Path(cfg.active_root) / "misc/plain.png", other / "plain again.png")
    cfg.roots = [cfg.active_root, str(other)]
    cfg.content_fingerprints = True
    _rescan(cfg)
    Scanner(cfg)._run(other, full=True)

    groups = fingerprints.duplicate_groups(conn)
    paths = [{(f["root"], f["rel_path"]) for f in g["files"]} for g in groups]
    assert {(cfg.active_root, "misc/plain.png"), (str(other), "plain again.png")} in paths
    assert {(cfg.active_root, "2023/05/12/shot0.jpg"),
            (cfg.active_root, "2023/05/12/shot0_copy.jpg")} in paths
    assert all(g["spare_bytes"] == g["size"] * (g["copies"] - 1) for g in groups)
    done = fingerprints.summary(conn)
    assert done["fingerprinted"] == done["files"] and len(done["roots"]) == 2


def test_a_stopped_pass_carries_on_where_it_was(scanned, monkeypatch):
    cfg, conn, _ = scanned
    monkeypatch.setattr(fingerprints, "GROUP", 3)
    stopped = {"now": False}

    def after_a_group(seen: int, total: int) -> None:
        stopped["now"] = True                # one group written, then stop
    first = fingerprints.fill(conn, cfg.active_root, stop=lambda: stopped["now"],
                              threads=1, progress=after_a_group)
    assert 0 < first["taken"] < first["owed"]
    second = fingerprints.fill(conn, cfg.active_root, threads=1)
    assert second["owed"] == first["owed"] - first["taken"]
    assert not fingerprints.owed_rows(conn, cfg.active_root)


def test_start_up_carries_on_fingerprinting_left_owed(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    assert scanner_mod.still_owed(conn, cfg.active_root, "fingerprints")
    _rescan(cfg)
    assert not scanner_mod.still_owed(conn, cfg.active_root, "fingerprints")


def test_the_console_reports_progress_and_copies(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.content_fingerprints = True
    _rescan(cfg)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        client = login(create_admin_app(services).test_client(), *ADMIN)
        got = client.get("/api/admin/fingerprints").get_json()
        assert got["enabled"] is True and got["algorithm"] == fingerprints.ALGO
        assert got["fingerprinted"] == got["files"] > 0
        assert any(len(g["files"]) == 2 for g in got["duplicates"])
        assert client.get("/api/admin/fingerprints?limit=x").status_code == 400
    finally:
        services.stop(timeout=5.0)


def test_without_blake3_the_fingerprints_say_what_they_are(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    monkeypatch.setattr(fingerprints, "_blake3", None)
    monkeypatch.setattr(fingerprints, "ALGO", "blake2b")
    _rescan(cfg)
    assert {r[0] for r in conn.execute("SELECT algo FROM content_fingerprints")} == {"blake2b"}
    monkeypatch.undo()
    # With BLAKE3 back, every one is owed again and made the faster way.
    if fingerprints.ALGO == "blake3":
        assert len(fingerprints.owed_rows(conn, cfg.active_root)) == conn.execute(
            "SELECT COUNT(*) FROM content_fingerprints").fetchone()[0]


def test_a_move_is_followed_on_the_job_engine(scanned):
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    _rescan(cfg)
    old = _row(conn, "shared/holiday/beach3.jpg")
    album = _album_with(conn, old["id"])
    root = Path(cfg.active_root)
    shutil.move(root / "shared/holiday/beach3.jpg", root / "misc/beach3.jpg")
    _rescan(cfg)
    assert _row(conn, "misc/beach3.jpg")["id"] == old["id"]
    assert conn.execute("SELECT COUNT(*) FROM album_items WHERE album_id=?",
                        (album,)).fetchone()[0] == 1
    assert not fingerprints.owed_rows(conn, cfg.active_root)


def test_spare_exact_copies_go_to_the_bin_and_the_cared_for_copy_stays(scanned):
    from ninaivu.storage import exact_copies
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.content_fingerprints = True
    _rescan(cfg)
    original, copy = _row(conn, "2023/05/12/shot0.jpg"), _row(conn, "2023/05/12/shot0_copy.jpg")
    # The copy is in an album: it is the one kept, though the other came first.
    _album_with(conn, copy["id"])
    plan = exact_copies.plan(conn, [cfg.active_root])
    group = next(g for g in plan["groups"]
                 if {f["id"] for f in [g["keep"], *g["set_aside"]]} == {original["id"], copy["id"]})
    assert group["keep"]["id"] == copy["id"] and [f["id"] for f in group["set_aside"]] == [original["id"]]
    assert plan["spare_files"] >= 1 and plan["spare_bytes"] >= original["size"]
    # Both in albums: left for a person.
    _album_with(conn, original["id"])
    again = next(g for g in exact_copies.plan(conn, [cfg.active_root])["groups"]
                 if g["fingerprint"] == group["fingerprint"])
    assert again["left_alone"] == "cared_for_twice" and again["set_aside"] == []

    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        client = login(create_admin_app(services).test_client(), *ADMIN)
        assert client.post("/api/admin/exact-copies/set-aside",
                           json={"password": "wrong"}).status_code == 401
        done = client.post("/api/admin/exact-copies/set-aside", json={"password": ADMIN[1]})
        assert done.status_code == 200, done.get_json()
        moved = done.get_json()["deleted"]
        assert moved == plan["spare_files"] - 1, (moved, plan["spare_files"])
        # The group left alone kept both copies.
        assert (Path(cfg.active_root) / "2023/05/12/shot0.jpg").exists()
        assert (Path(cfg.active_root) / "2023/05/12/shot0_copy.jpg").exists()
        assert client.get("/api/admin/exact-copies").get_json()["spare_files"] == 0
    finally:
        services.stop(timeout=5.0)
