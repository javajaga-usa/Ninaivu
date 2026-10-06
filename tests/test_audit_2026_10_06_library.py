"""Regressions for the library findings of the 2026-10-06 audit."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ninaivu import archive                                     # noqa: E402
from ninaivu.archive import database as adb                     # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                  # noqa: E402


# --------------------------------------------------------------------------
# A-03: a new photo at a path archived before
# --------------------------------------------------------------------------

@pytest.fixture()
def archive_work(tmp_path):
    archive.configure(tmp_path)
    adb.close_db()
    adb.init_db()
    try:
        yield tmp_path
    finally:
        adb.close_db()


def _archived(dest):
    out = {}
    for base, _, files in os.walk(dest):
        for name in files:
            if not name.lower().endswith('.jpg'):
                continue
            with open(os.path.join(base, name), 'rb') as fh:
                out[os.path.relpath(os.path.join(base, name), dest)] = fh.read()
    return out


def _photo(path, payload, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(b'\xff\xd8\xff\xe0' + payload + b'\xff\xd9')
    os.utime(path, (mtime, mtime))


def test_a_reformatted_card_at_the_same_path_is_archived_again(archive_work):
    card = str(archive_work / 'card')
    dest = str(archive_work / 'archive')
    photo = os.path.join(card, 'DCIM', 'IMG_0001.JPG')
    _photo(photo, b'FIRST' * 20000, 1_600_000_000)
    ArchiveJob([card], dest).run()
    first = _archived(dest)
    assert len(first) == 1

    # Formatted and shot again: same path, different bytes.
    _photo(photo, b'SECOND-SHOT' * 9000, 1_700_000_000)
    job = ArchiveJob([card], dest)
    job.run()
    second = _archived(dest)
    assert len(second) == 2, second
    assert any(b'SECOND-SHOT' in data for data in second.values())
    assert any(b'FIRST' in data for data in second.values())

    # The same bytes again are still a duplicate of the earlier copy, not a
    # third file: the replaced row stays a dedup target.
    other = os.path.join(card, 'DCIM', 'IMG_0002.JPG')
    _photo(other, b'FIRST' * 20000, 1_600_000_000)
    ArchiveJob([card], dest).run()
    assert len(_archived(dest)) == 2

    # And a plain re-run is still idempotent.
    ArchiveJob([card], dest).run()
    assert len(_archived(dest)) == 2


def test_a_clock_shift_on_a_fat_card_is_not_a_new_file(archive_work):
    row = {'size': 10, 'source_mtime': 1_600_000_000.0}
    assert adb._same_source_file(row, 10, 1_600_000_000.0 + 3600)
    assert adb._same_source_file(row, 10, 1_600_000_001.5)
    assert not adb._same_source_file(row, 11, 1_600_000_000.0)
    assert not adb._same_source_file(row, 10, 1_600_000_000.0 + 1800)
    # Nothing recorded (an old archive.db): trusted as before.
    assert adb._same_source_file({'size': None, 'source_mtime': None}, 10, 5.0)


# --------------------------------------------------------------------------
# A-04: a library folder is there only when its own marker is
# --------------------------------------------------------------------------

import json                                                     # noqa: E402
import shutil                                                   # noqa: E402
import sqlite3                                                  # noqa: E402
import time                                                     # noqa: E402
from pathlib import Path                                        # noqa: E402
from types import SimpleNamespace                               # noqa: E402

from ninaivu.storage import library_id, roots                   # noqa: E402


@pytest.fixture(autouse=True)
def _forget_the_state_dir():
    before = roots._state_dir
    roots.forget()
    yield
    roots._state_dir = before
    roots.forget()


def _unplug(root: Path) -> Path:
    """What a ``nofail`` mount looks like with its drive away: the folder is
    there and empty. The library is kept aside to plug back in."""
    away = root.with_name(root.name + "-unplugged")
    root.rename(away)
    root.mkdir()
    return away


def _plug_back(root: Path, away: Path) -> None:
    shutil.rmtree(root)
    away.rename(root)
    roots.forget()


def test_a_library_is_present_only_with_its_own_marker(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.jpg").write_bytes(b"x")
    cfg = SimpleNamespace(state_dir=state, roots=[str(lib)], active_root=str(lib))
    library_id.relocate(cfg, mounts=lambda: [])
    ident = library_id.read_id(lib)
    assert ident and roots.root_present(lib, state_dir=state)

    away = _unplug(lib)
    assert lib.is_dir() and not roots.root_present(lib, state_dir=state)
    assert not roots.available(lib)
    # Not given a new id in the empty folder, and the record is kept.
    library_id.relocate(cfg, mounts=lambda: [])
    assert library_id.read_id(lib) is None
    assert json.loads((state / library_id.KNOWN).read_text())[str(lib)] == ident

    # Another disk mounted at the path is not the library either.
    (lib / library_id.MARKER).write_text(json.dumps({"id": "someone-else"}))
    assert not roots.root_present(lib, state_dir=state)

    _plug_back(lib, away)
    assert roots.root_present(lib, state_dir=state)
    # A marker somebody deleted, with the photographs still there, is fine.
    (lib / library_id.MARKER).unlink()
    assert roots.root_present(lib, state_dir=state)
    # And a folder nothing was recorded for is judged as before.
    other = tmp_path / "new-lib"
    other.mkdir()
    assert roots.root_present(other, state_dir=state)


@pytest.fixture()
def marked(scanned):
    """A scanned library whose marker and id are recorded, as at start."""
    cfg, conn, scanner = scanned
    library_id.relocate(cfg, mounts=lambda: [])
    return cfg, conn, Path(cfg.active_root)


def test_the_storage_check_does_not_call_an_unplugged_library_missing(marked):
    from ninaivu.api import admin_api
    cfg, conn, root = marked
    away = _unplug(root)
    admin_api._run_scrubber(cfg.db_path)
    assert admin_api._SCRUBBER_PROGRESS["unavailable"] > 0
    assert admin_api._SCRUBBER_PROGRESS["missing"] == 0
    assert conn.execute("SELECT COUNT(*) FROM bitrot_records WHERE status='missing'"
                        ).fetchone()[0] == 0
    _plug_back(root, away)


def test_repair_does_not_write_into_an_unplugged_library(marked, tmp_path):
    from ninaivu.api import admin_api
    from ninaivu.storage import db
    from ninaivu.storage.mirror import Mirror
    from ninaivu.storage.repair import Repairer
    cfg, conn, root = marked
    admin_api._run_scrubber(cfg.db_path)                  # fingerprints first
    cfg.mirror_dir = str(tmp_path / "mirror")
    Path(cfg.mirror_dir).mkdir()
    mirror = Mirror(cfg, lambda: db.connect(cfg.db_path))
    rel = conn.execute("SELECT rel_path FROM assets WHERE filename='shot1.jpg'").fetchone()[0]
    copy = mirror._on_disk(Path(cfg.mirror_dir), str(root), rel)
    copy.parent.mkdir(parents=True)
    copy.write_bytes((root / rel).read_bytes())

    # Most of the library gone at once (the check ran while the drive was
    # half there, say): that is a disk away, not damage.
    for path in list(root.rglob("*.jpg")):
        path.unlink()
    admin_api._run_scrubber(cfg.db_path)
    repairer = Repairer(cfg, lambda: db.connect(cfg.db_path), mirror=mirror)
    repairer.start()
    repairer.join(30)
    assert not (root / rel).exists()
    assert repairer.status()["repaired"] == 0
    assert "missing" in repairer.status()["problems"][0]["why"]

    # And with the drive unplugged entirely, nothing is written either.
    away = _unplug(root)
    repairer.start()
    repairer.join(30)
    assert not any(root.iterdir())
    _plug_back(root, away)


def test_the_second_copy_refuses_an_empty_folder_where_its_disk_was(scanned, tmp_path):
    from ninaivu.storage import db
    from ninaivu.storage import mirror as mirror_mod
    cfg, conn, _ = scanned
    disk = tmp_path / "second-disk"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    mirror = mirror_mod.Mirror(cfg, lambda: db.connect(cfg.db_path))
    mirror.start()
    mirror.join(30)
    copied = conn.execute("SELECT COUNT(*) FROM mirror_copies").fetchone()[0]
    assert copied > 0

    away = disk.with_name("second-disk-away")
    disk.rename(away)
    disk.mkdir()                                          # the bare mount point
    mirror.start()
    mirror.join(30)
    assert "not there" in mirror.status()["error"]
    assert not any(disk.iterdir()), "nothing written onto the system disk"
    assert conn.execute("SELECT COUNT(*) FROM mirror_copies").fetchone()[0] == copied


def test_new_files_are_not_saved_into_an_unplugged_library(marked):
    from ninaivu.storage import new_files
    cfg, conn, root = marked
    away = _unplug(root)
    with pytest.raises(OSError, match="not there"):
        new_files.destination(cfg, str(root))
    _plug_back(root, away)
    assert new_files.destination(cfg, str(root)) == str(root)


# --------------------------------------------------------------------------
# A-40: a copy carrying the library's id is not adopted on its own
# --------------------------------------------------------------------------

def test_a_folder_found_by_id_waits_for_a_person(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    lib = tmp_path / "Volumes" / "Photos" / "lib"
    lib.mkdir(parents=True)
    (lib / "a.jpg").write_bytes(b"x")
    cfg = SimpleNamespace(state_dir=state, roots=[str(lib)], active_root=str(lib))
    library_id.relocate(cfg, mounts=lambda: [])
    backup = tmp_path / "Volumes" / "Backup" / "lib"
    shutil.copytree(lib, backup)                         # a manual copy, same id
    shutil.rmtree(lib.parent)                            # the drive unplugged
    moves = []
    assert library_id.relocate(cfg, look=lambda root: [backup], mounts=lambda: [],
                               move=lambda *a: moves.append(a)) == []
    assert moves == [] and cfg.roots == [str(lib)]
    waiting = library_id.pending_relocations(state)
    assert waiting[str(lib)]["found"] == [str(backup)]
    assert waiting[str(lib)]["confirmed"] is None


# --------------------------------------------------------------------------
# A-13: temporary files are never written through a link
# --------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="needs symlinks without privileges")
def test_a_link_at_a_temporary_name_is_refused(tmp_path):
    from ninaivu.utils.files import create_new
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    link = tmp_path / ".photo.jpg.ninaivu-part"
    link.symlink_to(outside)
    with pytest.raises(OSError, match="link"):
        create_new(link)
    assert outside.read_text() == "keep me"
    stale = tmp_path / ".stale.part"
    stale.write_text("half")
    with create_new(stale) as out:
        out.write(b"whole")
    assert stale.read_bytes() == b"whole"


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks without privileges")
def test_the_second_copy_does_not_restore_through_a_linked_folder(scanned, tmp_path):
    from ninaivu.storage import db
    from ninaivu.storage import mirror as mirror_mod
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    disk = tmp_path / "second-disk"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    mirror = mirror_mod.Mirror(cfg, lambda: db.connect(cfg.db_path))
    mirror.start()
    mirror.join(30)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(root / "shared")
    (root / "shared").symlink_to(elsewhere, target_is_directory=True)
    mirror.restore()
    mirror.join(30)
    assert not any(elsewhere.rglob("*.jpg"))


# --------------------------------------------------------------------------
# A-39: a name the second-copy disk takes for another file's
# --------------------------------------------------------------------------

def test_a_case_twin_on_the_second_copy_gets_a_name_of_its_own(scanned, tmp_path, monkeypatch):
    from ninaivu.storage import db
    from ninaivu.storage import mirror as mirror_mod
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    (root / "misc" / "Twin.png").write_bytes(b"first twin")
    (root / "misc" / "twin.png").write_bytes(b"second twin")
    disk = tmp_path / "second-disk"
    disk.mkdir()
    cfg.mirror_dir = str(disk)
    mirror = mirror_mod.Mirror(cfg, lambda: db.connect(cfg.db_path))
    mconn = mirror._db()
    mirror._know_the_disk(mconn, disk)
    mirror._copy_one(disk, str(root), "misc/Twin.png", mconn)
    folder = mirror._on_disk(disk, str(root), "misc/Twin.png").parent
    # A disk that ignores case: "twin.png" opens "Twin.png", and the folder
    # lists only the name it was made with.
    (folder / "twin.png").symlink_to("Twin.png")
    real = os.listdir
    monkeypatch.setattr(os, "listdir", lambda p=".": [
        n for n in real(p) if not os.path.islink(os.path.join(p, n))])
    size, digest, stored = mirror._copy_one(disk, str(root), "misc/twin.png", mconn)
    assert stored.endswith("misc/twin (2).png")
    assert (folder / "Twin.png").read_bytes() == b"first twin"
    assert (disk / stored).read_bytes() == b"second twin"
    assert not [n for n in real(folder) if "before" in n]


# --------------------------------------------------------------------------
# A-17 and A-38: importing again after losing the library; names any disk takes
# --------------------------------------------------------------------------

def test_an_import_brings_back_what_the_library_lost(scanned, tmp_path):
    from PIL import Image
    from ninaivu.storage import db
    from ninaivu.storage.importer import Importer
    cfg, conn, _ = scanned
    export = tmp_path / "export"
    export.mkdir()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(export / "beach.jpg")
    Image.new("RGB", (64, 48), (9, 8, 7)).save(export / "10:30 sunset?.jpg")
    importer = Importer(cfg, lambda: db.connect(cfg.db_path))

    def run():
        assert importer.start(str(export), cfg.active_root, None) == {"started": True}
        importer.join(60)
        return importer.status()

    first = run()
    assert first["copied"] == 2, first
    root = Path(cfg.active_root)
    landed = ["1030 sunset.jpg", "beach.jpg"]
    assert sorted(p.name for p in root.rglob("*.jpg") if p.name in landed) == landed
    for path in root.rglob("*.jpg"):
        if path.name in landed:
            path.unlink()
    second = run()
    assert second["copied"] == 2, second
    assert len([p for p in root.rglob("*.jpg") if p.name in landed]) == 2
    third = run()
    assert third["copied"] == 0


def test_portable_names():
    from ninaivu.utils.filenames import portable_name
    assert portable_name("a:b?.jpg") == "ab.jpg"
    assert portable_name("con.jpg") == "_con.jpg"
    assert portable_name("???.png", "photo") == "photo.png"
    assert portable_name("trailing. ") == "trailing"
    assert portable_name("நாள்.jpg") == "நாள்.jpg"


# --------------------------------------------------------------------------
# A-18: re-rooting moves every record that names the folder
# --------------------------------------------------------------------------

def test_reroot_moves_the_bin_second_copy_and_import_records(tmp_path):
    from ninaivu.storage import db, reroot
    from ninaivu.storage import importer as importer_mod
    from ninaivu.storage import mirror as mirror_mod
    old, new = str(tmp_path / "old"), str(tmp_path / "new")
    conn = db.init_db(tmp_path / "index.db")
    conn.executescript(mirror_mod.SCHEMA)
    conn.executescript(importer_mod.SCHEMA)
    conn.execute("INSERT INTO recycled(asset_id, root, rel_path, filename, size, bin_path, "
                 "deleted_at) VALUES(1, ?, 'a.jpg', 'a.jpg', 1, ?, 0)",
                 (old, os.path.join(old, "_deleted", "2026-10-06", "a.jpg")))
    conn.execute("INSERT INTO mirror_copies(root, rel_path) VALUES(?, 'a.jpg')", (old,))
    conn.execute("INSERT INTO mirror_copies(root, rel_path) VALUES(?, 'b.jpg')", (old,))
    conn.execute("INSERT INTO mirror_copies(root, rel_path) VALUES(?, 'b.jpg')", (new,))
    conn.execute("INSERT INTO imports(key, state, root, rel_path) VALUES('k', 'copied', ?, 'a.jpg')",
                 (old,))
    conn.commit()
    reroot.rewrite_paths(conn, old, new, [])
    assert tuple(conn.execute("SELECT root, bin_path FROM recycled").fetchone()) == \
        (new, os.path.join(new, "_deleted", "2026-10-06", "a.jpg"))
    assert sorted(tuple(r) for r in conn.execute("SELECT root, rel_path FROM mirror_copies")) == \
        [(new, "a.jpg"), (new, "b.jpg")]
    assert conn.execute("SELECT root FROM imports").fetchone()[0] == new


# --------------------------------------------------------------------------
# A-20: copy-to-drive syncs, checks, and does not trust name and size alone
# --------------------------------------------------------------------------

def test_a_full_size_file_with_other_bytes_is_not_taken_for_the_copy(tmp_path):
    import threading
    from ninaivu.utils import drives
    src = tmp_path / "src.jpg"
    src.write_bytes(b"real photograph")
    dest = tmp_path / "drive" / "src.jpg"
    dest.parent.mkdir()
    dest.write_bytes(b"\0" * len(b"real photograph"))     # pulled out before the flush
    os.utime(dest, (1, 1))
    place = drives._place(str(dest), src.stat().st_size, str(src))
    assert place == str(tmp_path / "drive" / "src (2).jpg")
    drives._copy(str(src), place, threading.Event())
    assert Path(place).read_bytes() == b"real photograph"
    # The same bytes under another time are there already.
    os.utime(place, (5, 5))
    assert drives._place(str(dest), src.stat().st_size, str(src)) is None


# --------------------------------------------------------------------------
# A-21: "already here" holds only while the library's copy does, and only
# among the sender's own files
# --------------------------------------------------------------------------

def _phone_photo():
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (5, 6, 7)).save(out, "PNG")
    return out.getvalue()


def _back_up(client, name, data):
    started = client.post("/api/phone-backup/files", json={
        "device_id": "phone-test-0001", "device": "phone", "name": name,
        "size": len(data), "modified": 1_500_000_000_000})
    assert started.status_code == 200, started.get_json()
    answer = client.put(f"/api/phone-backup/files/{started.get_json()['id']}?offset=0",
                        data=data, content_type="application/octet-stream")
    assert answer.status_code == 200, answer.get_json()
    return answer.get_json()


def test_a_phone_backup_already_here_is_sent_again_once_its_twin_is_gone(app, people):
    from conftest import FAMILY, login
    from ninaivu.media import phone_backup
    family = login(app.test_client(), *FAMILY)
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    original = (Path(cfg.active_root) / "misc" / "plain.png").read_bytes()
    done = _back_up(family, "copy-of-plain.png", original)
    assert done["state"] == phone_backup.DUPLICATE
    conn.execute("UPDATE assets SET trashed=1 WHERE filename='plain.png'")
    conn.commit()
    asked = family.post("/api/phone-backup/check", json={
        "device_id": "phone-test-0001",
        "files": [{"name": "copy-of-plain.png", "size": len(original),
                   "modified": 1_500_000_000_000}]}).get_json()
    assert asked["files"][0]["state"] == phone_backup.FAILED   # to be sent again


def test_a_twin_outside_the_senders_folder_is_not_already_here(app, people):
    from conftest import FAMILY, login
    from ninaivu.media import phone_backup
    from ninaivu.server import auth
    auth.update_profile(people["conn"], people["family"].id, scope="shared")
    family = login(app.test_client(), *FAMILY)
    cfg = app.config["MV_CONFIG"]
    original = (Path(cfg.active_root) / "misc" / "plain.png").read_bytes()
    done = _back_up(family, "copy-of-plain.png", original)
    assert done["state"] == phone_backup.STAGED


# --------------------------------------------------------------------------
# A-19: a file of a size the device cannot state is not taken as arrived
# --------------------------------------------------------------------------

def test_a_staged_file_of_unknown_size_is_fetched_again(tmp_path, monkeypatch):
    from ninaivu.utils import devices
    target = tmp_path / "staging"
    target.mkdir()
    (target / "IMG_1.JPG").write_bytes(b"cut sho")          # cut off last time
    fetched = []
    monkeypatch.setattr(devices, "unavailable_reason", lambda: None)
    monkeypatch.setattr(devices, "walk", lambda *a, **k: iter([
        {"name": "IMG_1.JPG", "path": "Phone\\DCIM\\IMG_1.JPG", "exact_size": None}]))

    def copy_one(path, dest_dir, name, expected, shown=0):
        assert not (dest_dir / name).exists(), "the old file must go first"
        fetched.append(name)
        (dest_dir / name).write_bytes(b"cut short no more")
    monkeypatch.setattr(devices, "_copy_one", copy_one)
    result = devices.copy_out("Phone\\DCIM", target)
    assert fetched == ["IMG_1.JPG"] and result["copied"] == 1
    assert (target / "IMG_1.JPG").read_bytes() == b"cut short no more"


# --------------------------------------------------------------------------
# A-37: the folder is synced before a copy is recorded
# --------------------------------------------------------------------------

def test_the_archive_syncs_the_folder_before_recording_a_copy(archive_work, monkeypatch):
    from ninaivu.archive import scanner as archive_scanner
    synced = []
    seen_when_recorded = []
    real_set_status = adb.set_status
    monkeypatch.setattr(archive_scanner, "sync_folder", lambda folder: synced.append(folder))

    def set_status(path, status, **fields):
        if status == 'copied':
            seen_when_recorded.append(len(synced))
        return real_set_status(path, status, **fields)
    monkeypatch.setattr(archive_scanner.db, "set_status", set_status)
    card = str(archive_work / 'card')
    _photo(os.path.join(card, 'IMG_0001.JPG'), b'A' * 70000, 1_600_000_000)
    ArchiveJob([card], str(archive_work / 'archive')).run()
    assert seen_when_recorded == [1]


# --------------------------------------------------------------------------
# A-41: deleting from a library that is the archive is not damage
# --------------------------------------------------------------------------

def test_a_photo_deleted_from_the_archive_library_stays_deleted(archive_work):
    card = str(archive_work / 'card')
    dest = archive_work / 'archive'
    _photo(os.path.join(card, 'IMG_0001.JPG'), b'B' * 70000, 1_600_000_000)
    ArchiveJob([card], str(dest)).run()
    landed = [p for p in dest.rglob('*.JPG')]
    assert len(landed) == 1
    assert adb.note_library_deletion([str(landed[0])]) == 1
    landed[0].unlink()
    ArchiveJob([card], str(dest)).run()
    assert not list(dest.rglob('*.JPG')), 'the deleted photograph came back'
    assert not adb.verified_rows()
    # Put back from the bin: the archive owns it again.
    assert adb.note_library_deletion([str(landed[0])], deleted=False) == 1


# --------------------------------------------------------------------------
# A-42: a delete cut short is finished or undone, never half-done
# --------------------------------------------------------------------------

def test_a_delete_cut_short_after_the_move_is_finished(scanned, monkeypatch):
    from ninaivu.storage import recycle
    cfg, conn, _ = scanned
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE filename='shot2.jpg'").fetchone()
    real_executemany = conn.executemany

    class PowerCut(BaseException):
        pass

    class Conn:
        def __getattr__(self, name):
            return getattr(conn, name)

        def executemany(self, sql, rows):
            if sql.startswith("DELETE FROM assets"):
                raise PowerCut()
            return real_executemany(sql, rows)

    monkeypatch.setattr(recycle.shutil, "move", shutil.move)
    with pytest.raises(PowerCut):
        recycle.recycle(Conn(), [row["id"]])
    # The rollback put the file back and dropped the entry: nothing half-done.
    assert (Path(row["root"]) / row["rel_path"]).exists()
    assert conn.execute("SELECT COUNT(*) FROM recycled").fetchone()[0] == 0

    # A real power cut: the entry is on disk and the file in the bin, and the
    # index still names it. The next look at the bin finishes the delete.
    conn.execute("INSERT INTO recycled(asset_id, root, rel_path, filename, size, bin_path, "
                 "deleted_at, metadata) VALUES(?,?,?,?,?,?,?,?)",
                 (row["id"], row["root"], row["rel_path"], "shot2.jpg", 1,
                  str(Path(row["root"]) / "_deleted" / "x" / "shot2.jpg"), time.time(), "{}"))
    conn.commit()
    binned = Path(row["root"]) / "_deleted" / "x" / "shot2.jpg"
    binned.parent.mkdir(parents=True)
    shutil.move(str(Path(row["root"]) / row["rel_path"]), binned)
    recycle.listing(conn)
    assert conn.execute("SELECT 1 FROM assets WHERE id=?", (row["id"],)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM recycled").fetchone()[0] == 1


# --------------------------------------------------------------------------
# A-43: the index backup links the pending uploads rather than copying them
# --------------------------------------------------------------------------

def test_the_backup_stages_beside_the_state_and_links_big_files(tmp_path, monkeypatch):
    from ninaivu.storage import backup
    state = tmp_path / "state"
    (state / "pending-uploads" / "abc").mkdir(parents=True)
    video = state / "pending-uploads" / "abc" / "clip.mp4"
    video.write_bytes(b"v" * 1000)
    staged = tmp_path / "staged"
    staged.mkdir()
    assert backup._stage(state, staged)
    assert os.path.samefile(video, staged / "pending-uploads" / "abc" / "clip.mp4")
    sqlite3.connect(state / "index.db").close()
    places = []
    real = backup.tempfile.TemporaryDirectory
    monkeypatch.setattr(backup.tempfile, "TemporaryDirectory",
                        lambda **kw: places.append(kw.get("dir")) or real(**kw))
    backup._snapshot(state, tmp_path / "out")
    assert places == [state]


def test_a_pending_relocation_can_be_listed_and_confirmed_from_the_console(as_admin, scanned):
    import json
    from pathlib import Path
    from ninaivu.storage import library_id
    admin_client = as_admin
    state = Path(scanned[0].state_dir)
    (state / library_id.PENDING).write_text(json.dumps(
        {"/old/lib": {"found": ["/new/lib"], "noticed_at": 1, "confirmed": None}}))
    got = admin_client.get("/api/admin/library/relocation").get_json()
    assert got["pending"]["/old/lib"]["found"] == ["/new/lib"]
    bad = admin_client.post("/api/admin/library/relocation", json={"old": "/old/lib", "new": "/elsewhere"})
    assert bad.status_code == 400
    ok = admin_client.post("/api/admin/library/relocation", json={"old": "/old/lib", "new": "/new/lib"})
    assert ok.status_code == 200 and ok.get_json()["restart_needed"]
    assert library_id.pending_relocations(state)["/old/lib"]["confirmed"] == "/new/lib"
