"""The library's change journal (2.0 proposal B, second stage): every change
to the index's list of files written down as it happens, a cursor to follow
it, a day-by-day summary, a warning for hundreds of files gone at once, and
on a Mac a start-up that reads only the folders the drive says changed.
Opt-in with ``change_journal``; off, nothing is written."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.media.scanner import Scanner
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, fsevents, journal


def _rescan(cfg, full: bool = False, only=None) -> Scanner:
    scanner = Scanner(cfg)
    scanner._run(Path(cfg.active_root), full=full, only=only)
    return scanner


def _kinds(conn, after: int = 0) -> list[tuple[str, str]]:
    return [(i["kind"], i["rel_path"]) for i in journal.changes_since(conn, after)["items"]]


@pytest.fixture()
def keeping(scanned):
    cfg, conn, _ = scanned
    cfg.change_journal = True
    journal.switch(conn, True)
    return cfg, conn


def test_off_by_default_and_on_the_advanced_page(scanned):
    assert Config().change_journal is False
    assert "change_journal" in GROUPS["Advanced"]
    cfg, conn, _ = scanned
    # The fixture's first scan ran with it off: the table is there, empty.
    assert journal.counts(conn) == {"rows": 0, "since": None, "latest": 0, "keeping": False}


def test_switching_puts_the_triggers_in_and_takes_them_out(scanned):
    _, conn, _ = scanned
    assert journal.switch(conn, True) is True and journal.keeping(conn)
    assert journal.switch(conn, True) is True             # twice is the same
    assert journal.switch(conn, False) is False and not journal.keeping(conn)


def test_a_new_file_an_edit_and_a_removal_are_written_down(keeping):
    cfg, conn = keeping
    root = Path(cfg.active_root)
    start = journal.latest(conn)
    Image.new("RGB", (320, 200), (1, 2, 3)).save(root / "2023/05/12/new.jpg")
    _rescan(cfg)
    assert _kinds(conn, start) == [("added", "2023/05/12/new.jpg")]

    mark = journal.latest(conn)
    Image.new("RGB", (330, 210), (9, 9, 9)).save(root / "2023/05/12/new.jpg")
    _rescan(cfg)
    assert _kinds(conn, mark) == [("edited", "2023/05/12/new.jpg")]

    mark = journal.latest(conn)
    (root / "2023/05/12/new.jpg").unlink()
    _rescan(cfg)
    changes = journal.changes_since(conn, mark)["items"]
    assert [(c["kind"], c["rel_path"]) for c in changes] == [("removed", "2023/05/12/new.jpg")]
    assert changes[0]["asset_id"] and changes[0]["item_kind"] == "picture"


def test_a_rescan_that_finds_nothing_writes_nothing(keeping):
    cfg, conn = keeping
    before = journal.latest(conn)
    _rescan(cfg)
    assert journal.latest(conn) == before


def test_a_move_and_a_rename_keep_the_row_and_say_which(keeping):
    cfg, conn = keeping
    cfg.content_fingerprints = True
    _rescan(cfg)                                          # fingerprints taken
    root = Path(cfg.active_root)
    mark = journal.latest(conn)
    shutil.move(root / "2023/05/12/shot0.jpg", root / "2023/05/12/renamed.jpg")
    (root / "moved").mkdir()
    shutil.move(root / "misc/plain.png", root / "moved/plain.png")
    _rescan(cfg)
    got = {(i["kind"], i["rel_path"], i.get("old_path")) for i in
           journal.changes_since(conn, mark)["items"]}
    assert ("renamed", "2023/05/12/renamed.jpg", "2023/05/12/shot0.jpg") in got
    assert ("moved", "moved/plain.png", "misc/plain.png") in got
    assert not any(kind in ("added", "removed") for kind, _, _ in got)


def test_a_removed_file_keeps_its_fingerprint_to_find_it_by(keeping):
    cfg, conn = keeping
    cfg.content_fingerprints = True
    _rescan(cfg)
    root = Path(cfg.active_root)
    digest = conn.execute(
        "SELECT f.digest FROM content_fingerprints f JOIN assets a ON a.id=f.asset_id "
        "WHERE a.rel_path='misc/plain.png'").fetchone()[0]
    mark = journal.latest(conn)
    (root / "misc/plain.png").unlink()
    _rescan(cfg)
    gone = journal.changes_since(conn, mark, kinds=["removed"])["items"]
    assert [g["digest"] for g in gone] == [digest]


def test_the_bin_is_binned_and_restored(keeping):
    cfg, conn = keeping
    aid = conn.execute("SELECT id FROM assets WHERE rel_path='misc/plain.png'").fetchone()[0]
    mark = journal.latest(conn)
    with db._write_lock:
        conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (aid,))
        conn.execute("UPDATE assets SET trashed=0 WHERE id=?", (aid,))
        conn.commit()
    assert [k for k, _ in _kinds(conn, mark)] == ["binned", "restored"]
    assert [h["kind"] for h in journal.history(conn, aid)][-2:] == ["binned", "restored"]


def test_off_nothing_is_written_and_what_was_stays(keeping):
    cfg, conn = keeping
    root = Path(cfg.active_root)
    Image.new("RGB", (320, 200)).save(root / "misc/one.jpg")
    _rescan(cfg)
    kept = journal.latest(conn)
    assert kept
    cfg.change_journal = False
    journal.switch(conn, False)
    Image.new("RGB", (320, 200)).save(root / "misc/two.jpg")
    _rescan(cfg)
    assert journal.latest(conn) == kept


def test_the_cursor_pages_and_notices_a_gap(keeping):
    cfg, conn = keeping
    root = Path(cfg.active_root)
    for i in range(5):
        Image.new("RGB", (200 + i, 200)).save(root / f"misc/n{i}.jpg")
    _rescan(cfg)
    first = journal.changes_since(conn, 0, limit=2)
    assert len(first["items"]) == 2 and first["more"] is True
    second = journal.changes_since(conn, first["next"], limit=10)
    assert len(second["items"]) == 3 and second["more"] is False
    assert {i["seq"] for i in first["items"]}.isdisjoint(i["seq"] for i in second["items"])
    journal.trim(conn, keep_rows=1)
    assert journal.changes_since(conn, first["next"])["gap"] is True


def test_trim_forgets_the_old(keeping):
    _, conn = keeping
    now = time.time()
    with db._write_lock:
        conn.executemany("INSERT INTO library_journal(at, kind, rel_path) VALUES (?,?,?)",
                         [(now - 400 * 86400, "added", "old.jpg"), (now, "added", "new.jpg")])
        conn.commit()
    assert journal.trim(conn, now=now) == 1
    assert [i["rel_path"] for i in journal.changes_since(conn)["items"]] == ["new.jpg"]


def _burst(conn, n: int, at: float, folder: str = "2026/10", kind: str = "removed") -> None:
    with db._write_lock:
        conn.executemany(
            "INSERT INTO library_journal(at, kind, root, rel_path, folder) VALUES (?,?,?,?,?)",
            [(at + i, kind, "/lib", f"{folder}/{i}.jpg", folder) for i in range(n)])
        conn.commit()


def test_hundreds_gone_at_once_is_a_burst_and_a_few_are_not(keeping):
    _, conn = keeping
    now = time.time()
    _burst(conn, 250, now - 3600)                         # within a few minutes
    _burst(conn, 20, now - 7200, folder="2025/01")
    _burst(conn, 300, now - 600, kind="added", folder="2024/02")   # an import
    found = journal.bursts(conn, now=now)
    assert [(b["kind"], b["area"], b["count"]) for b in found] == [("removed", "2026", 250)]


def test_the_summary_is_a_line_per_day_kind_and_folder(keeping):
    _, conn = keeping
    now = time.time()
    _burst(conn, 3, now - 60, folder="2026/10/19", kind="added")
    _burst(conn, 2, now - 30, folder="2026/10/18", kind="added")
    lines = journal.summary(conn, now=now)
    added = [x for x in lines if x["kind"] == "added" and x["root"] == "/lib"]
    assert len(added) == 1 and added[0]["area"] == "2026/10" and added[0]["count"] == 5


def test_the_console_shows_it(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.change_journal = True
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        assert journal.keeping(conn), "start-up puts the triggers in"
        Image.new("RGB", (320, 200)).save(Path(cfg.active_root) / "misc/console.jpg")
        _rescan(cfg)
        client = login(create_admin_app(services).test_client(), *ADMIN)
        got = client.get("/api/admin/journal").get_json()
        assert got["enabled"] is True and got["keeping"] is True and got["rows"] >= 1
        assert any(d["kind"] == "added" for d in got["days"])
        changes = client.get("/api/admin/journal/changes?after=0&kind=added").get_json()
        assert changes["items"][-1]["rel_path"] == "misc/console.jpg"
        aid = changes["items"][-1]["asset_id"]
        assert client.get(f"/api/admin/journal/item/{aid}").get_json()["items"][0]["kind"] == "added"
        assert client.get("/api/admin/journal?days=x").status_code == 400
        # Switched off on the console: the triggers come out at once.
        assert client.post("/api/admin/settings/all", json={"change_journal": False}).status_code == 200
        assert not journal.keeping(conn)
    finally:
        services.stop(timeout=5.0)


# --- start-up on a Mac -------------------------------------------------------

class FakeDrive:
    """Stands in for storage/fsevents.py: a drive with a record of changes."""

    def __init__(self, folders=(), *, complete=True, volume="VOL-1", now=500):
        self.folders, self.complete, self.volume, self.now = folders, complete, volume, now
        self.asked: list[int] = []

    def available(self):
        return True

    def volume_id(self, path):
        return self.volume

    def current_event_id(self):
        return self.now

    def changes_since(self, paths, since, **_):
        self.asked.append(since)
        return fsevents.History(frozenset(self.folders), self.complete, self.now,
                                "" if self.complete else "dropped")


def test_no_record_yet_reads_everything(keeping):
    _, conn = keeping
    assert journal.plan_start_up(conn, "/lib", fse=FakeDrive()) is None


def test_the_drive_names_the_folders_that_changed(keeping, tmp_path):
    cfg, conn = keeping
    root = cfg.active_root
    journal.remember_mark(conn, root, {"volume": "VOL-1", "event": 100, "at": time.time()},
                          whole=True)
    drive = FakeDrive([f"{root}/2023/05/12", f"{root}/misc", "/elsewhere/x"])
    plan = journal.plan_start_up(conn, root, fse=drive)
    assert plan is not None and plan.folders == {"2023/05/12", "misc"}
    assert drive.asked == [100] and plan.mark["event"] == 500


@pytest.mark.parametrize("why", ["incomplete", "other drive", "stale", "root itself"])
def test_anything_uncertain_reads_everything(keeping, why):
    cfg, conn = keeping
    root = cfg.active_root
    walked = time.time() - (30 * 86400 if why == "stale" else 0)
    journal.remember_mark(conn, root, {"volume": "VOL-1", "event": 100, "at": walked}, whole=True)
    drive = FakeDrive([root] if why == "root itself" else [f"{root}/misc"],
                      complete=why != "incomplete",
                      volume="VOL-2" if why == "other drive" else "VOL-1")
    assert journal.plan_start_up(conn, root, fse=drive) is None


def test_a_full_walk_keeps_the_mark_and_a_watcher_scan_does_not(keeping, monkeypatch):
    cfg, conn = keeping
    drive = FakeDrive(now=900)
    monkeypatch.setattr(journal, "_fsevents", lambda: drive)
    _rescan(cfg, only=["misc"])
    assert journal.stored_mark(conn, cfg.active_root) is None
    _rescan(cfg)
    assert journal.stored_mark(conn, cfg.active_root)["event"] == 900
    walked = float(db.get_meta(conn, journal.WALKED_KEY.format(root=cfg.active_root)))
    assert walked > time.time() - 60


def test_start_up_reads_only_what_the_drive_names(keeping, monkeypatch):
    """The whole round on a pretend Mac: a mark from the last full walk, two
    files changed while Ninaivu was off, a start-up that reads only their
    folder, and the mark kept from that reading."""
    import ninaivu

    cfg, conn = keeping
    root = cfg.active_root
    journal.remember_mark(conn, root, {"volume": "VOL-1", "event": 100, "at": time.time()},
                          whole=True)
    Image.new("RGB", (320, 200)).save(Path(root) / "misc/while-off.jpg")
    drive = FakeDrive([f"{root}/misc"], now=700)
    monkeypatch.setattr(journal, "_fsevents", lambda: drive)
    monkeypatch.setattr(ninaivu.sys, "platform", "darwin")
    services = build_services(cfg)
    services.scanner.stop(join=True)
    started: list[tuple] = []
    monkeypatch.setattr(services.scanner, "start",
                        lambda roots, **kw: started.append((roots, kw)))
    monkeypatch.setattr(services.scanner, "watch", lambda *a, **k: None)
    try:
        services._scan_the_libraries()
        assert started == [([Path(root)], {"scopes": {Path(root): frozenset({"misc"})}})]
        # The scan those folders were handed to keeps the drive's mark.
        reader = Scanner(cfg)
        reader.journal_marks = services.scanner.journal_marks
        reader._run(Path(root), full=False, only=["misc"])
        assert journal.stored_mark(conn, root)["event"] == 700
        assert ("added", "misc/while-off.jpg") in _kinds(conn)
        # Nothing changed the next time: nothing is read at all.
        started.clear()
        drive.folders, drive.now = (), 800
        services._scan_the_libraries()
        assert started == []
        assert journal.stored_mark(conn, root)["event"] == 800
    finally:
        services.stop(timeout=5.0)
