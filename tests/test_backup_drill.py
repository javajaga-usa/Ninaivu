"""The backup drill (ninaivu/copies/drill.py): a few photographs restored from
every copy into a temporary folder and held to the library's fingerprints."""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.copies import drill
from ninaivu.storage import db, fingerprints
from ninaivu.storage.mirror import SCHEMA as MIRROR_SCHEMA


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def drilled(scanned, tmp_path):
    """A fingerprinted library, and a second disk holding three of its files."""
    cfg, conn, _ = scanned
    for root in cfg.roots:
        fingerprints.fill(conn, root)
    conn.executescript(MIRROR_SCHEMA)
    rows = [dict(r) for r in conn.execute(
        "SELECT a.root, a.rel_path, a.size, a.mtime FROM assets a "
        "JOIN content_fingerprints f ON f.asset_id = a.id WHERE a.trashed = 0 "
        "ORDER BY a.id LIMIT 3")]
    assert len(rows) == 3
    disk = tmp_path / "second-disk"
    for row in rows:
        source = Path(row["root"]).joinpath(*row["rel_path"].split("/"))
        copy = disk.joinpath(*row["rel_path"].split("/"))
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, copy)
        conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime, sha256, copied_at) "
                     "VALUES (?,?,?,?,?,?)", (row["root"], row["rel_path"], row["size"],
                                             row["mtime"], _sha(copy), time.time()))
    conn.commit()

    mirror = SimpleNamespace(
        enabled=True, folder=disk, problem=lambda: None,
        copy_path=lambda root, rel: disk.joinpath(*rel.split("/")))
    return SimpleNamespace(cfg=cfg, conn=conn, rows=rows, disk=disk, mirror=mirror)


def _drill(lib, **engines):
    return drill.BackupDrill(lib.cfg, connect_db=lambda: db.connect(lib.cfg.db_path),
                             pick=lambda pool, n: list(pool)[:n], **engines)


def _copy(result, name):
    return next(c for c in result["copies"] if c["name"] == name)


def _library_bytes(lib) -> dict[str, str]:
    return {r["rel_path"]: _sha(Path(r["root"]).joinpath(*r["rel_path"].split("/")))
            for r in lib.rows}


def test_a_good_second_disk_passes_against_the_fingerprints(drilled):
    before = _library_bytes(drilled)
    result = _drill(drilled, mirror=drilled.mirror).run()
    mirror = _copy(result, "mirror")
    assert result["status"] == drill.PASSED
    assert mirror["status"] == drill.PASSED
    assert mirror["checked"] == mirror["passed"] == mirror["fingerprinted"] == 3
    # Nothing in the library changed, and the temporary folder is gone.
    assert _library_bytes(drilled) == before
    assert not (Path(drilled.cfg.state_dir) / "backup-drill").exists()


def test_a_copy_that_agrees_with_its_own_record_but_not_the_photograph_fails(drilled):
    """The case only the drill catches: the copy's own checksum was taken of
    the wrong bytes, so the copy's own check would call it fine."""
    row = drilled.rows[0]
    copy = drilled.disk.joinpath(*row["rel_path"].split("/"))
    data = bytearray(copy.read_bytes())
    data[len(data) // 2] ^= 0xFF
    copy.write_bytes(bytes(data))
    drilled.conn.execute("UPDATE mirror_copies SET sha256=? WHERE rel_path=?",
                         (_sha(copy), row["rel_path"]))
    drilled.conn.commit()

    result = _drill(drilled, mirror=drilled.mirror).run()
    mirror = _copy(result, "mirror")
    assert result["status"] == drill.FAILED
    assert mirror["failed"] == 1
    bad = next(f for f in mirror["files"] if not f["ok"])
    assert bad["path"] == row["rel_path"]
    assert bad["why"] == "it does not match the library's fingerprint"


def test_a_file_missing_from_the_disk_fails(drilled):
    row = drilled.rows[1]
    drilled.disk.joinpath(*row["rel_path"].split("/")).unlink()
    mirror = _copy(_drill(drilled, mirror=drilled.mirror).run(), "mirror")
    assert mirror["status"] == drill.FAILED
    assert {"path": row["rel_path"], "ok": False, "why": "it is not on the disk"} in mirror["files"]


def test_a_file_changed_since_it_was_copied_is_held_to_the_copys_record(drilled):
    """The library's fingerprint is of a newer version: it cannot judge the
    copy, so the copy's own record does, and the answer says so."""
    row = drilled.rows[2]
    drilled.conn.execute("UPDATE mirror_copies SET size = size + 1 WHERE rel_path=?",
                         (row["rel_path"],))
    drilled.conn.commit()
    path = drilled.disk.joinpath(*row["rel_path"].split("/"))
    with open(path, "ab") as handle:
        handle.write(b"x")
    drilled.conn.execute("UPDATE mirror_copies SET sha256=? WHERE rel_path=?",
                         (_sha(path), row["rel_path"]))
    drilled.conn.commit()
    mirror = _copy(_drill(drilled, mirror=drilled.mirror).run(), "mirror")
    changed = next(f for f in mirror["files"] if f["path"] == row["rel_path"])
    assert changed == {"path": row["rel_path"], "ok": True, "checked": "changed since"}
    assert mirror["status"] == drill.PASSED and mirror["fingerprinted"] == 2


def test_an_unplugged_disk_is_said_and_is_not_a_pass(drilled, tmp_path):
    drilled.mirror.folder = tmp_path / "not-there"
    result = _drill(drilled, mirror=drilled.mirror).run()
    assert _copy(result, "mirror")["status"] == drill.ATTENTION
    assert result["status"] == drill.ATTENTION


def test_with_no_copy_set_up_there_is_nothing_to_check(drilled):
    drilled.mirror.enabled = False
    result = _drill(drilled, mirror=drilled.mirror).run()
    assert result["status"] == drill.NOTHING
    assert {c["status"] for c in result["copies"]} == {drill.OFF}


def test_the_off_site_copy_and_the_index_are_asked_too(drilled):
    offsite = SimpleNamespace(enabled=True, problem=lambda: None, restore_sample=lambda n: {
        "ok": False, "checked": 2, "files": [
            {"path": "a.jpg", "ok": True, "checked": "blake3"},
            {"path": "b.jpg", "ok": False, "checked": "blake3", "why": "the fingerprint differs"}]})
    backups = SimpleNamespace(verify_latest=lambda: {"ok": True, "bundle": "ninaivu_1.tar.gz",
                                                     "assets": 12, "error": None})
    result = _drill(drilled, mirror=drilled.mirror, offsite=offsite, backups=backups).run()
    assert _copy(result, "offsite")["status"] == drill.FAILED
    assert _copy(result, "offsite")["fingerprinted"] == 1
    index = _copy(result, "index")
    assert index["status"] == drill.PASSED and index["items"] == 12
    assert result["status"] == drill.FAILED


def test_one_copy_failing_to_be_asked_does_not_stop_the_others(drilled):
    def broken(_n):
        raise RuntimeError("boom")
    offsite = SimpleNamespace(enabled=True, problem=lambda: None, restore_sample=broken)
    result = _drill(drilled, mirror=drilled.mirror, offsite=offsite).run()
    assert _copy(result, "offsite")["status"] == drill.FAILED
    assert "boom" in _copy(result, "offsite")["reason"]
    assert _copy(result, "mirror")["status"] == drill.PASSED


def test_the_last_drills_are_kept(drilled):
    runner = _drill(drilled, mirror=drilled.mirror)
    for _ in range(drill.KEEP + 2):
        runner.run()
    status = runner.status()
    assert status["running"] is False
    assert status["last"]["status"] == drill.PASSED
    assert len(runner.history()) == drill.KEEP


def test_the_console_starts_one_and_shows_it(app, as_admin):
    services = app.config["MV_SERVICES"]
    services.backup_drill._pick = lambda pool, n: list(pool)[:n]
    answer = as_admin.post("/api/admin/backup-drill")
    assert answer.status_code == 200 and answer.get_json()["started"] is True
    deadline = time.time() + 30
    while services.backup_drill.running and time.time() < deadline:
        time.sleep(0.05)
    status = as_admin.get("/api/admin/backup-drill").get_json()
    assert status["running"] is False
    assert status["last"]["status"] in (drill.NOTHING, drill.PASSED, drill.ATTENTION)
    assert [c["name"] for c in status["last"]["copies"]] == list(drill.ORDER)


def test_the_console_drill_is_for_administrators(as_family, anon):
    for client in (as_family, anon):
        assert client.get("/api/admin/backup-drill").status_code in (401, 403)
        assert client.post("/api/admin/backup-drill").status_code in (401, 403)


def test_google_drive_not_connected_is_off_and_a_lost_key_fails(drilled):
    from ninaivu.cloud import store

    away = SimpleNamespace(creds=SimpleNamespace(connected=False))
    assert _copy(_drill(drilled, cloud=away).run(), "drive")["status"] == drill.OFF

    store.init_schema(drilled.conn)
    row = drilled.rows[0]
    drilled.conn.execute(
        "INSERT INTO cloud_uploads(root, rel_path, size, state, remote_id, encrypted, digest) "
        "VALUES (?,?,?,?,?,?,?)", (row["root"], row["rel_path"], row["size"], store.DONE,
                                  "remote-1", 1, "0" * 64))
    drilled.conn.commit()
    here = SimpleNamespace(creds=SimpleNamespace(connected=True), cfg=drilled.cfg)
    drive = _copy(_drill(drilled, cloud=here).run(), "drive")
    assert drive["status"] == drill.FAILED
    assert drive["files"][0]["why"] == "the key is not on this computer"
