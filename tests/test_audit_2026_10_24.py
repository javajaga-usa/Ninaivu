"""Tenth audit (after the 3.0 work, F-01..F-11): two computers, exact copies,
the drives screen and the Mugil set-up.

Each test names the finding it holds to; the report is
project-audit/ninaivu-audit-2026-10-24.md in the project's files.
"""

import io
import shutil
import time
from pathlib import Path

import pytest

from conftest import ADMIN
from test_mugil_two_computers import (  # noqa: F401 - fixtures used by name
    _paired, _ready_to_free, keep, partner_through, primary, run, secondary, two_apps,
)
from ninaivu.mugil import free, inbox as inbox_mod, pairing
from ninaivu.mugil.courier import PartnerError
from ninaivu.mugil.hold import DISK_ID, LEDGER, Holding, folder_problem
from ninaivu.storage import db, recycle


# --- F-01: an unplugged originals disk is not the system disk ------------------------

def _unplug(folder: Path) -> None:
    """What an unplugged disk under a ``nofail`` mount leaves: its empty folder."""
    for entry in folder.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def test_an_empty_folder_where_the_originals_disk_was_is_not_used(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    folder = tmp_path / "mnt" / "n300"
    folder.mkdir(parents=True)
    assert folder_problem(str(folder), [], state) is None
    assert (folder / DISK_ID).is_file()
    keep(Holding(folder), "2024/a.jpg", b"one")
    _unplug(folder)
    said = folder_problem(str(folder), [], state)
    assert said and "not plugged in" in said
    # Nothing was made there by asking.
    assert not any(folder.iterdir())
    # Another disk with an id of its own mounted at the place is a disk of its own.
    (folder / DISK_ID).write_text("another-disk\n")
    assert folder_problem(str(folder), [], state) is None


def test_the_secondary_answers_away_not_none_kept(primary, secondary, scanned):  # noqa: F811
    mine, courier = primary
    _, folder = secondary
    assert run(courier)["sent"] > 0
    _unplug(folder)
    partner = courier.partner(mine.mugil_partner_url, pairing.load(mine.state_dir)["key"])
    hello = partner.request("GET", "/api/mugil/hello")
    assert hello["originals_ready"] is False
    with pytest.raises(PartnerError) as refused:
        partner.request("POST", "/api/mugil/have", body={
            "library": "x" * 8, "items": [{"path": "a.jpg", "size": 1, "algo": "blake3",
                                           "digest": "0"}]})
    assert refused.value.status == 503
    # No fresh record of nothing was started on the empty folder.
    assert not (folder / LEDGER).exists()


# --- F-02: deleting a let-go photograph -------------------------------------------------

def test_a_let_go_original_is_brought_back_to_be_deleted(  # noqa: F811
        two_apps, tmp_path, scanned, monkeypatch):  # noqa: F811
    from ninaivu.mugil import fetch
    primary_app, secondary_app, other = two_apps
    first, services, folder, later = _ready_to_free(two_apps, tmp_path, scanned)
    cfg = scanned[0]
    through = partner_through(secondary_app.test_client())
    monkeypatch.setattr(fetch, "Partner", through)
    services.mugil_free.partner = through
    first.post("/api/admin/mugil/role", json={"role": "primary", "free_space": True})
    done = services.mugil_free.run(now=later, limit=1)
    assert done["freed"] == 1, done
    conn = db.connect(cfg.db_path)
    gone = free.let_go(conn)[0]
    row = conn.execute("SELECT id FROM assets WHERE root=? AND rel_path=?",
                       (gone["root"], gone["rel_path"])).fetchone()
    answer = first.post("/api/admin/folders/delete", json={"ids": [row["id"]]})
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["deleted"] == 1, answer.get_json()
    # In the bin like any other, to be put back from there; nothing let go is left
    # to bring back into the library, and unpairing is not blocked by it.
    assert free.summary(conn)["files"] == 0
    binned = conn.execute("SELECT bin_path FROM recycled WHERE asset_id=?",
                          (row["id"],)).fetchone()
    assert binned is not None and Path(binned["bin_path"]).is_file()


def test_the_bin_refuses_a_let_go_original_it_cannot_hold(scanned):
    cfg, conn, _ = scanned
    row = conn.execute("SELECT * FROM assets WHERE kind='picture' ORDER BY id LIMIT 1").fetchone()
    path = Path(row["root"]) / row["rel_path"]
    conn.execute("INSERT INTO mugil_let_go(root, rel_path, size, mtime, algo, digest, let_go_at) "
                 "VALUES (?,?,?,?,?,?,?)", (row["root"], row["rel_path"], row["size"],
                                             row["mtime"], "blake3", "0" * 64, time.time()))
    conn.commit()
    path.unlink()
    result = recycle.recycle(conn, [row["id"]], user_id=None, roots=[row["root"]])
    assert result["deleted"] == 0 and "other computer" in result["failed"][0]["why"]
    assert conn.execute("SELECT 1 FROM assets WHERE id=?", (row["id"],)).fetchone() is not None
    assert free.summary(conn)["files"] == 1


# --- F-03: a Secondary that keeps originals does not wait for any Primary by itself ----

def test_a_secondary_keeping_originals_does_not_open_a_wait_at_start(app, tmp_path):
    services = app.config["MV_SERVICES"]
    cfg = services.cfg
    folder = tmp_path / "n300"
    folder.mkdir()
    cfg.mugil_role, cfg.mugil_originals_dir = "secondary", str(folder)
    services.mugil_welcome.close()
    services._wait_for_a_primary()
    assert services.mugil_welcome.waiting        # a new one: nothing to read yet
    services.mugil_welcome.close()
    keep(Holding(folder), "2024/a.jpg", b"one")
    services._wait_for_a_primary()
    assert not services.mugil_welcome.waiting
    services.mugil_beacon.stop()
    services.mugil_waker.stop()


# --- F-04: the phone inbox is collected past files that stay -----------------------------

class _InboxPartner:
    """The Secondary's inbox routes, answered from an Inbox here."""

    def __init__(self, box, page=2):
        self.box, self.page = box, page

    def request(self, method, path, *, query=None, body=None, **_):
        if path == "/api/mugil/inbox/keys":
            return {"keys": self.box.set_keys(body["keys"])}
        if path == "/api/mugil/inbox":
            return {"items": self.box.waiting(limit=self.page,
                                              after=int((query or {}).get("after", 0)))}
        if path == "/api/mugil/inbox/taken":
            return {"taken": self.box.taken(body["ids"])}
        raise AssertionError(path)

    def download(self, path, *, query, write):
        found, _ = self.box.file_of(int(query["id"]))
        data = found.read_bytes()
        write(data)
        return len(data)


def _png(colour):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (32, 24), colour).save(out, "PNG")
    return out.getvalue()


def test_files_that_stay_do_not_hold_back_the_rest(scanned, tmp_path):
    from ninaivu.media import phone_keys
    from ninaivu.server import auth
    cfg, conn, _ = scanned
    auth.init_auth_schema(conn)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    dad = conn.execute("SELECT id FROM users WHERE username=?", (ADMIN[0],)).fetchone()[0]
    phone_keys.init_schema(conn)
    key_row, _key = phone_keys.make(conn, dad, "Dad's phone")
    box = inbox_mod.Inbox(tmp_path / "originals")
    # Three from a key the Primary no longer knows, sent first, then one of Dad's.
    for n in range(3):
        data = _png((n, 0, 0))
        box.receive(999, f"old/{n}.png", f"{n}.png", io.BytesIO(data),
                    length=len(data), limit=1 << 30)
    data = _png((0, 200, 0))
    box.receive(int(key_row["id"]), "2024/new.png", "new.png", io.BytesIO(data),
                length=len(data), limit=1 << 30)
    totals = inbox_mod.collect(cfg, conn, _InboxPartner(box))
    assert totals["collected"] == 1, totals
    assert totals["left"] == 3
    assert [i["path"] for i in box.waiting()] == ["old/0.png", "old/1.png", "old/2.png"]


# --- F-05: the drives screen knows the Secondary's originals disk ------------------------

def test_the_originals_disk_is_not_offered_for_import(cfg, tmp_path, monkeypatch):
    from ninaivu.api import drives_api
    from ninaivu.utils import drives
    monkeypatch.setattr(drives_api, "_archive_settings", lambda: {})
    disk = tmp_path / "n300"
    (disk / "originals").mkdir(parents=True)
    cfg.mugil_originals_dir = str(disk / "originals")
    found = drives.Drive(id="d1", path=str(disk), label="N300", total=1, free=1)
    assert drives_api.what_it_holds(found, cfg) == "copy"


# --- F-06: exact copies seen by different people, or told a story -----------------------

def _two_copies(scanned):
    from ninaivu.storage import fingerprints
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    fingerprints.fill(conn, cfg.active_root, threads=1)
    group = next(g for g in fingerprints.duplicate_groups(conn)
                 if {f["rel_path"] for f in g["files"]} >= {"2023/05/12/shot0.jpg",
                                                          "2023/05/12/shot0_copy.jpg"})
    return cfg, conn, group


def test_copies_shown_to_different_people_are_left_alone(scanned):
    from ninaivu.storage import exact_copies
    cfg, conn, group = _two_copies(scanned)
    one = group["files"][0]["id"]
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility='hidden' WHERE id=?", (one,))
        conn.commit()
    found = next(g for g in exact_copies.plan(conn, [cfg.active_root])["groups"]
                 if g["fingerprint"] == group["fingerprint"])
    assert found["left_alone"] == "seen_differently" and found["set_aside"] == []


def test_a_copy_with_a_voice_story_is_the_one_kept(scanned):
    from ninaivu.storage import exact_copies, stories
    cfg, conn, group = _two_copies(scanned)
    told = group["files"][-1]["id"]
    conn.executescript(stories.SCHEMA)
    with db._write_lock:
        conn.execute("INSERT INTO stories(asset_id, file, mime, size, created_at) "
                     "VALUES (?, 'a.m4a', 'audio/mp4', 1, 0)", (told,))
        conn.commit()
    found = next(g for g in exact_copies.plan(conn, [cfg.active_root])["groups"]
                 if g["fingerprint"] == group["fingerprint"])
    assert found["keep"]["id"] == told and told not in {f["id"] for f in found["set_aside"]}


# --- F-07: the checklist counts the files that are in the library now --------------------

def test_the_originals_item_is_not_ticked_by_files_deleted_since(scanned, tmp_path):
    from ninaivu.mugil import checklist, courier
    cfg, conn, _ = scanned
    cfg.state_dir = tmp_path / "checklist-state"
    cfg.state_dir.mkdir()
    root = str(cfg.roots[0])
    rows = conn.execute("SELECT rel_path, size, mtime FROM assets WHERE root=? AND trashed=0",
                        (root,)).fetchall()
    with courier.sent_record(cfg.state_dir) as record:
        # Every file but one sent, and as many again that were deleted since.
        record.executemany(
            "INSERT INTO sent(root, rel_path, size, mtime, algo, digest, sent_at) "
            "VALUES (?,?,?,?,?,?,0)",
            [(root, r["rel_path"], r["size"], r["mtime"], "blake3", "0") for r in rows[1:]]
            + [(root, f"gone/{n}.jpg", 1, 0, "blake3", "0") for n in range(len(rows))])
    total, confirmed = checklist._originals_confirmed(cfg, conn, courier)
    assert total == len(rows) and confirmed == len(rows) - 1


# --- F-08: freeing stops when it is turned off -------------------------------------------

def test_freeing_stops_when_it_is_turned_off(two_apps, tmp_path, scanned, monkeypatch):  # noqa: F811
    from ninaivu.mugil import fetch
    from ninaivu.server.settings_groups import MANAGED
    primary_app, secondary_app, other = two_apps
    first, services, folder, later = _ready_to_free(two_apps, tmp_path, scanned)
    through = partner_through(secondary_app.test_client())
    monkeypatch.setattr(fetch, "Partner", through)
    services.mugil_free.partner = through
    first.post("/api/admin/mugil/role", json={"role": "primary", "free_space": True})
    real = services.mugil_free._one

    def one_then_off(*args, **kwargs):
        services.cfg.mugil_free_space = False
        return real(*args, **kwargs)
    monkeypatch.setattr(services.mugil_free, "_one", one_then_off)
    done = services.mugil_free.run(now=later)
    assert done["freed"] == 1, done
    assert "mugil_free_space" in MANAGED and "mugil_family_copy" in MANAGED


# --- F-10: a let-go original is not taken for a file that moved ----------------------------

def test_a_copy_of_a_let_go_original_does_not_take_its_row(scanned):
    from ninaivu.media.scanner import Scanner
    from ninaivu.storage import fingerprints
    cfg, conn, _ = scanned
    cfg.content_fingerprints = True
    cfg.watch = False
    fingerprints.fill(conn, cfg.active_root, threads=1)
    row = conn.execute("SELECT * FROM assets WHERE rel_path='misc/plain.png'").fetchone()
    path = Path(cfg.active_root) / row["rel_path"]
    shutil.copy2(path, Path(cfg.active_root) / "misc/plain again.png")
    conn.execute("INSERT INTO mugil_let_go(root, rel_path, size, mtime, algo, digest, let_go_at) "
                 "VALUES (?,?,?,?,?,?,?)", (row["root"], row["rel_path"], row["size"],
                                             row["mtime"], "blake3", "0" * 64, time.time()))
    conn.commit()
    path.unlink()
    Scanner(cfg)._run(Path(cfg.active_root), full=False)
    kept = conn.execute("SELECT rel_path FROM assets WHERE id=?", (row["id"],)).fetchone()
    assert kept is not None and kept["rel_path"] == "misc/plain.png"
    assert conn.execute("SELECT 1 FROM assets WHERE rel_path='misc/plain again.png'").fetchone()


# --- F-11: each run's connection to the index is closed ------------------------------------

def test_a_run_closes_its_own_connection(primary, secondary, scanned):  # noqa: F811
    import sqlite3
    mine, courier = primary
    made = []

    def connect():
        conn = sqlite3.connect(scanned[0].db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        made.append(conn)
        return conn
    courier.connect_db = connect
    assert courier.start()["started"]
    courier._thread.join(timeout=60)
    assert made and courier.last.get("complete") is True, courier.last
    with pytest.raises(sqlite3.ProgrammingError):
        made[0].execute("SELECT 1")
