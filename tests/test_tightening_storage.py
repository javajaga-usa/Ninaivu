"""The tightening pass over storage/ and cli/: loose ends that each let
something quietly go wrong — a hidden photograph shown, an import that could
never finish, a backup check that said nothing, records that only grew."""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ninaivu.cli import backup_restore
from ninaivu.server import auth, runfile
from ninaivu.storage import backup, db, disk_health, importer as importer_mod
from ninaivu.storage import mirror as mirror_mod
from ninaivu.storage import reroot as reroot_kit
from ninaivu.storage import roots as roots_kit
from ninaivu.storage.importer import Importer
from ninaivu.storage.recycle import Gone


def _jpeg(colour) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 30), colour).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture()
def index(tmp_path):
    path = tmp_path / "state" / "index.db"
    conn = db.init_db(path)
    auth.init_auth_schema(conn)
    return path, conn


def _asset(conn, rel, root="/lib", visibility=1, source="default", kind="picture"):
    db.bulk_upsert(conn, [{"root": root, "rel_path": rel, "filename": rel.rpartition("/")[2],
                           "folder": rel.rpartition("/")[0], "kind": kind,
                           "visibility": visibility, "vis_source": source}])
    return conn.execute("SELECT * FROM assets WHERE root=? AND rel_path=?",
                        (root, rel)).fetchone()


# -- DS-15: hidden in iCloud is hidden from the moment it is indexed ----------------

def test_an_imported_photo_hidden_in_icloud_is_hidden_as_it_is_indexed(index, tmp_path):
    path, conn = index
    Importer(SimpleNamespace(roots=["/lib"]), lambda: db.connect(path))._db()
    conn.execute("INSERT INTO imports(key, state, root, rel_path, hidden, applied, source) "
                 "VALUES('k', 'copied', '/lib', '2019/12/17/IMG_2.JPG', 1, 0, 'icloud')")
    conn.commit()
    row = _asset(conn, "2019/12/17/IMG_2.JPG")
    assert (row["visibility"], row["vis_source"]) == (2, "item"), \
        "hidden in the same transaction that indexed it, before any scan said so"
    assert _asset(conn, "2019/12/17/IMG_3.JPG")["visibility"] == 1, "nothing else"
    db.upsert_asset(conn, {"root": "/lib", "rel_path": "2019/12/17/IMG_2.JPG",
                           "filename": "IMG_2.JPG", "kind": "picture"})
    assert conn.execute("SELECT visibility FROM assets WHERE rel_path='2019/12/17/IMG_2.JPG'"
                        ).fetchone()[0] == 2


def test_without_any_import_indexing_is_unchanged(index):
    _, conn = index
    assert _asset(conn, "a.jpg")["visibility"] == 1


def test_the_import_is_written_down_before_the_file_takes_its_name(tmp_path, monkeypatch):
    partial = tmp_path / "x.part"
    partial.write_bytes(b"photo")
    seen = []

    def claim(candidate):
        seen.append((candidate.name, candidate.exists()))

    target = importer_mod._publish(partial, tmp_path / "a.jpg", claim)
    assert target.read_bytes() == b"photo"
    assert seen == [("a.jpg", False)], "told the name before it existed"


# -- S-4: one damaged part of an export does not stop the import --------------------

@pytest.fixture()
def export(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    folder = tmp_path / "exports"
    folder.mkdir()
    with zipfile.ZipFile(folder / "takeout-001.zip", "w") as z:
        z.writestr("Takeout/Google Photos/Photos from 2019/good.jpg", _jpeg((10, 120, 200)))
    # A member whose compressed bytes were damaged: zlib, not OSError.
    damaged = folder / "takeout-002.zip"
    payload = b"".join(os.urandom(50) + b"b" * 5000 for _ in range(200))
    with zipfile.ZipFile(damaged, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Takeout/Google Photos/Photos from 2019/bad.jpg", payload)
    raw = bytearray(damaged.read_bytes())
    for i in range(100, 164):
        raw[i] = 0xFF
    damaged.write_bytes(bytes(raw))
    # A .tgz whose download was cut short: EOFError.
    tgz = folder / "takeout-003.tgz"
    with tarfile.open(tgz, "w:gz") as tar:
        for name in ("one.jpg", "two.jpg"):
            data = os.urandom(200_000)
            info = tarfile.TarInfo(f"Takeout/Google Photos/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    tgz.write_bytes(tgz.read_bytes()[:150_000])
    path = tmp_path / "state" / "index.db"
    db.init_db(path)
    return SimpleNamespace(roots=[str(library)]), path, folder, library


def test_a_damaged_zip_or_cut_short_tgz_does_not_stop_the_good_parts(export, monkeypatch):
    cfg, path, folder, library = export
    monkeypatch.setattr(roots_kit, "root_present", lambda root, **kw: True)
    job = Importer(cfg, lambda: db.connect(path))
    job._run(folder, str(library), None)
    state = job.status()
    assert not state["error"], state
    assert state["copied"] >= 1, state
    assert list(library.rglob("good.jpg")), "the good zip's photograph came in"
    assert state["failed"] >= 2, state["problems"]


# -- S-1: undoing a folder change covers what arrived since -------------------------

def test_undo_puts_files_that_arrived_since_back_under_the_old_rule(index):
    _, conn = index
    db.set_folder_visibility(conn, "/lib", "Private", 2, record_undo=False)
    _asset(conn, "Private/a.jpg", visibility=2, source="folder")
    db.set_folder_visibility(conn, "/lib", "Private", 1)          # the mistake
    _asset(conn, "Private/b.jpg", visibility=1, source="folder")  # arrives meanwhile
    _asset(conn, "Elsewhere/c.jpg", visibility=1, source="default")
    assert db.undo_visibility_batch(conn)["ok"]
    levels = {r["rel_path"]: r["visibility"] for r in conn.execute(
        "SELECT rel_path, visibility FROM assets")}
    assert levels == {"Private/a.jpg": 2, "Private/b.jpg": 2, "Elsewhere/c.jpg": 1}


def test_undo_of_a_new_rule_lets_later_files_go_back_to_the_default(index):
    _, conn = index
    db.set_folder_visibility(conn, "/lib", "Trip", 0)
    _asset(conn, "Trip/new.jpg", visibility=0, source="folder")
    db.undo_visibility_batch(conn)
    row = conn.execute("SELECT visibility, vis_source FROM assets").fetchone()
    assert (row[0], row[1]) == (1, "default")


def test_undo_that_hides_again_forgets_what_ai_read(index):
    _, conn = index
    row = _asset(conn, "secret.jpg", visibility=2, source="item")
    db.set_visibility(conn, [row["id"]], 1)                      # shown by mistake
    conn.execute("UPDATE assets SET ocr_text='account 1234', ocr_version=1 WHERE id=?",
                 (row["id"],))
    conn.commit()
    db.undo_visibility_batch(conn)
    after = conn.execute("SELECT visibility, ocr_text FROM assets WHERE id=?",
                         (row["id"],)).fetchone()
    assert (after[0], after[1]) == (2, None)


# -- S-3: a re-root moves every record kept by folder -------------------------------

def test_reroot_moves_the_offsite_erased_and_approval_records(tmp_path):
    from ninaivu.cloud import approvals, offsite

    old, new = r"E:\Photos", r"D:\Photos"
    state = tmp_path / "state"
    conn = db.init_db(state / "index.db")
    auth.init_auth_schema(conn)
    conn.executescript(offsite.SCHEMA)
    conn.executescript(approvals.SCHEMA)
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind) VALUES(?, 'a.jpg', "
                 "'a.jpg', 'picture')", (old,))
    conn.execute("INSERT INTO offsite_copies(root, rel_path, size, mtime, object) "
                 "VALUES(?, 'a.jpg', 5, 1, 'files/aa/x.ninaivu')", (old,))
    conn.execute("INSERT INTO erased(root, rel_path, size, erased_at) "
                 "VALUES(?, 'gone.jpg', 7, 1)", (old,))
    conn.execute("INSERT INTO cloud_approvals(root, rel_path, size, decision, decided_at) "
                 "VALUES(?, 'big.mp4', 9, 'approved', 1)", (old,))
    conn.commit()
    reroot_kit.reroot(state, old, new, dry_run=False)
    check = db.connect(state / "index.db")
    for table in ("offsite_copies", "erased", "cloud_approvals"):
        roots = {r[0] for r in check.execute(f"SELECT root FROM {table}")}
        assert roots == {new}, table
    assert Gone(check).holds(new, "gone.jpg", 7), "an erased photograph stays erased"


# -- S-5 and S-8: backups -------------------------------------------------------------

def _cut_bundle(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    bundle = folder / f"{backup.PREFIX}20260101_000000_x.tar.gz"
    data = os.urandom(300_000)
    with tarfile.open(bundle, "w:gz") as tar:
        info = tarfile.TarInfo("state/index.db")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    bundle.write_bytes(bundle.read_bytes()[:100_000])
    return bundle


def test_a_damaged_bundle_is_reported_not_raised(tmp_path):
    result = backup.verify_bundle(_cut_bundle(tmp_path / "b"))
    assert result["ok"] is False and result["error"]


def test_checking_a_damaged_bundle_replaces_the_old_verdict(tmp_path):
    folder = tmp_path / "backups"
    _cut_bundle(folder)
    (folder / backup.VERIFIED_FILE).write_text(json.dumps({"ok": True}))
    keeper = backup.BackupKeeper(SimpleNamespace(backup_dir=str(folder),
                                                 state_dir=str(tmp_path)))
    assert keeper.verify_latest()["ok"] is False
    assert keeper.last_verification()["ok"] is False


def test_a_bundle_left_half_written_is_cleared_away(tmp_path):
    state = tmp_path / "state"
    db.init_db(state / "index.db")
    out = tmp_path / "backups"
    out.mkdir()
    old = out / f"{backup.PARTIAL_PREFIX}abc.tmp"
    old.write_bytes(b"half a bundle")
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    fresh = out / f"{backup.PARTIAL_PREFIX}def.tmp"
    fresh.write_bytes(b"another backup writing now")
    assert backup.snapshot(state, out) is not None
    assert not old.exists() and fresh.exists()


# -- S-6: storage-check rows do not grow for ever ------------------------------------

def test_old_storage_check_rows_are_pruned_and_the_ones_read_are_kept(index, tmp_path):
    path, conn = index
    one = _asset(conn, "one.jpg")["id"]
    two = _asset(conn, "two.jpg")["id"]
    for status, actual in (("baseline", "h1"), ("verified", "h1"), ("verified", "h1")):
        db.record_bitrot_check(conn, one, "/lib", "one.jpg", "h1", actual, status)
    db.record_bitrot_check(conn, two, "/lib", "two.jpg", "h2", "h2", "baseline")
    db.record_bitrot_check(conn, two, "/lib", "two.jpg", "h2", None, "missing")
    before_one = db.last_bitrot_fingerprint(conn, one)
    before_two = db.last_bitrot_fingerprint(conn, two)
    summary = db.get_bitrot_summary(conn)

    backup.tidy_records(SimpleNamespace(db_path=str(path), state_dir=str(tmp_path)))

    check = db.connect(path)
    assert check.execute("SELECT COUNT(*) FROM bitrot_records").fetchone()[0] == 3
    assert db.last_bitrot_fingerprint(check, one) == before_one
    assert db.last_bitrot_fingerprint(check, two) == before_two
    assert db.get_bitrot_summary(check) == summary


# -- S-7: a stopped restore leaves nothing half-written in the library ---------------

def test_a_restore_that_runs_out_of_room_leaves_no_part_file(tmp_path, monkeypatch):
    library = tmp_path / "Photos"
    library.mkdir()
    copy = tmp_path / "copy"
    copy.mkdir()
    path = tmp_path / "state" / "index.db"
    db.init_db(path)
    cfg = SimpleNamespace(mirror_dir=str(copy), library_roots=[str(library)],
                          roots=[str(library)], state_dir=str(tmp_path / "state"))
    keeper = mirror_mod.Mirror(cfg, lambda: db.connect(path))
    conn = keeper._db()
    keeper._know_the_disk(conn, copy)
    stored = keeper._on_disk(copy, str(library), "2019/a.jpg")
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"x" * 1000)
    conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime, sha256) "
                 "VALUES(?, '2019/a.jpg', 1000, 0, '')", (str(library),))
    conn.commit()
    monkeypatch.setattr(roots_kit, "root_present", lambda root, **kw: True)
    real = mirror_mod.create_new

    class Full:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, data):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(mirror_mod, "create_new", lambda p: Full(real(p)))
    keeper._restore()
    assert not list(library.rglob("*.ninaivu-part"))
    assert not (library / "2019" / "a.jpg").exists()


# -- S-9: a repair's Drive download that runs out of time is stopped ------------------

def test_a_drive_download_past_its_hour_is_stopped(tmp_path, monkeypatch):
    from ninaivu.cloud import restore
    from ninaivu.storage.repair import Repairer

    stopped = []

    class Endless:
        def __init__(self, **kw):
            self.running = False
            self.state = SimpleNamespace(snapshot=lambda: {"message": "slow"})

        def start(self):
            self.running = True

        def join(self, timeout=None):
            pass

        def stop(self, join=False):
            stopped.append(join)
            self.running = False

    monkeypatch.setattr(restore, "from_record", lambda conn, **kw: [
        SimpleNamespace(rel_path="a.jpg", mtime=1.0)])
    monkeypatch.setattr(restore, "RestoreJob", Endless)
    ticks = iter(range(0, 10**9, 4000))
    cloud = SimpleNamespace(creds=SimpleNamespace(connected=True), client=None,
                            restore_key=lambda items: None)
    db.init_db(tmp_path / "i.db")
    repairer = Repairer(SimpleNamespace(state_dir=str(tmp_path)),
                        lambda: db.connect(tmp_path / "i.db"), cloud=cloud,
                        clock=lambda: float(next(ticks)))
    tried: list[str] = []
    assert repairer._from_drive({"root": "/lib", "asset_id": 1}, "h", tried) is None
    assert stopped == [True]
    assert any("more than an hour" in t for t in tried)


# -- S-10: a drive probe that keeps failing is said once ------------------------------

def test_a_probe_that_keeps_failing_warns_once(tmp_path, caplog):
    db.init_db(tmp_path / "i.db")

    def broken(since):
        raise RuntimeError("PowerShell is not available")

    watch = disk_health.DiskWatch(SimpleNamespace(roots=[], state_dir=""),
                                  lambda: db.connect(tmp_path / "i.db"), probe=broken)
    with caplog.at_level(logging.WARNING, logger="ninaivu.storage.disk_health"):
        for _ in range(3):
            assert "PowerShell" in watch.check()["error"]
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


# -- S-11: restore asks the server's own lock -----------------------------------------

def test_restore_refuses_while_the_server_lock_is_held(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with runfile.server_lock(state):
        with pytest.raises(ValueError, match="Stop Ninaivu"):
            backup_restore._require_stopped(state)
    backup_restore._require_stopped(state)        # let go: no complaint
