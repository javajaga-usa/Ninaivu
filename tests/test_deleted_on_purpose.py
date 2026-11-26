"""A photograph removed on purpose is not "missing", and nothing brings it back.

Deleted in Ninaivu, it leaves ``recycled`` and then ``erased``. Removed in
Finder, it left nothing: the storage check called it missing, the automatic
repair put it back from the second copy, and the Secondary could bring it
back once a scan had forgotten it (storage/on_purpose.py).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ninaivu.api import admin_api
from ninaivu.media.scanner import Scanner
from ninaivu.mugil.bring_in import _deleted_here
from ninaivu.storage import db, on_purpose, recycle
from ninaivu.storage.mirror import Mirror
from ninaivu.storage.repair import Repairer, candidates


@pytest.fixture()
def lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    cfg.mirror_dir = str(tmp_path / "mirror")
    Path(cfg.mirror_dir).mkdir()
    mirror = Mirror(cfg, lambda: db.connect(cfg.db_path))
    root = cfg.active_root
    row = conn.execute("SELECT id, rel_path, size FROM assets WHERE filename='shot1.jpg'"
                       ).fetchone()
    path = Path(root) / row["rel_path"]
    admin_api._run_scrubber(cfg.db_path)            # fingerprints first
    copy = mirror._on_disk(Path(cfg.mirror_dir), root, row["rel_path"])
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(path.read_bytes())
    return {"cfg": cfg, "conn": conn, "mirror": mirror, "root": root, "id": row["id"],
            "rel": row["rel_path"], "size": row["size"], "path": path}


def removed_in_finder(lib):
    lib["path"].unlink()
    admin_api._run_scrubber(lib["cfg"].db_path)


def repairer(lib):
    return Repairer(lib["cfg"], lambda: db.connect(lib["cfg"].db_path), mirror=lib["mirror"])


def test_the_automatic_repair_never_puts_back_a_file_that_was_removed(lib):
    removed_in_finder(lib)
    assert [r["id"] for r in on_purpose.gone(lib["conn"])] == [lib["id"]]
    assert candidates(lib["conn"]) == [], "a missing file is not owed a repair by itself"
    fixer = repairer(lib)
    fixer.after_check()
    fixer.join(30)
    assert not lib["path"].exists()
    status = fixer.status()
    assert status["waiting"] == 0 and status["gone"] == 1


def test_said_to_be_removed_on_purpose_it_is_forgotten_and_never_comes_back(lib):
    removed_in_finder(lib)
    result = on_purpose.mark_removed(lib["conn"])
    assert result["removed"] == 1 and result["kept"] == []
    conn = lib["conn"]
    assert conn.execute("SELECT 1 FROM assets WHERE id=?", (lib["id"],)).fetchone() is None
    assert db.get_bitrot_summary(conn)["missing"] == 0, "no longer listed as missing"
    assert db.current_bitrot_issues(conn) == []
    assert on_purpose.deleted_on_purpose(conn, lib["root"], lib["rel"])
    assert _deleted_here(conn, lib["root"], lib["rel"], lib["size"]), \
        "the Secondary's bring-in leaves it where it is"
    fixer = repairer(lib)
    fixer.start(missing=True)
    fixer.join(30)
    assert not lib["path"].exists()


def test_put_back_when_asked(lib):
    removed_in_finder(lib)
    fixer = repairer(lib)
    fixer.start(missing=True)
    fixer.join(30)
    assert lib["path"].is_file(), fixer.status()


def test_a_file_is_not_taken_as_removed_while_its_disk_is_away_or_it_is_back(lib, tmp_path):
    removed_in_finder(lib)
    away = tmp_path / "away"
    os.rename(lib["root"], away)
    try:
        result = on_purpose.mark_removed(lib["conn"])
        assert result["removed"] == 0 and "plugged in" in result["kept"][0]["why"]
    finally:
        os.rename(away, lib["root"])
    lib["path"].write_bytes(b"back again")
    result = on_purpose.mark_removed(lib["conn"], [lib["id"]])
    assert result["removed"] == 0 and "back where it was" in result["kept"][0]["why"]
    assert lib["conn"].execute("SELECT 1 FROM assets WHERE id=?", (lib["id"],)).fetchone()


def test_a_scan_that_forgets_a_removed_file_records_it(lib):
    lib["path"].unlink()
    Scanner(lib["cfg"])._run(Path(lib["root"]), full=True)
    conn = lib["conn"]
    assert conn.execute("SELECT 1 FROM assets WHERE id=?", (lib["id"],)).fetchone() is None
    assert _deleted_here(conn, lib["root"], lib["rel"], lib["size"])


def test_deleting_in_ninaivu_counts_as_on_purpose(lib):
    conn = lib["conn"]
    assert recycle.recycle(conn, [lib["id"]])["deleted"] == 1
    assert on_purpose.deleted_on_purpose(conn, lib["root"], lib["rel"])
    assert db.get_bitrot_summary(conn)["missing"] == 0


def test_a_repair_refuses_a_place_recorded_as_deleted(lib):
    removed_in_finder(lib)
    conn = lib["conn"]
    with db._write_lock:
        on_purpose.record_erased(conn, [{"root": lib["root"], "rel_path": lib["rel"],
                                         "size": lib["size"]}])
        conn.commit()
    fixer = repairer(lib)
    fixer.start(missing=True)
    fixer.join(30)
    assert not lib["path"].exists()
    assert "on purpose" in fixer.status()["problems"][0]["why"]


def test_the_console_says_so(scanned, tmp_path):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    admin_api._run_scrubber(cfg.db_path)
    row = conn.execute("SELECT id, rel_path FROM assets WHERE filename='shot1.jpg'").fetchone()
    (Path(cfg.active_root) / row["rel_path"]).unlink()
    admin_api._run_scrubber(cfg.db_path)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        client = login(create_admin_app(services).test_client(), *ADMIN)
        assert client.get("/api/admin/scrubber/status").get_json()["missing"] == 1
        assert client.post("/api/admin/scrubber/removed", json={"ids": "x"}).status_code == 400
        assert client.post("/api/admin/repair", json={"missing": "yes"}).status_code == 400
        data = client.post("/api/admin/scrubber/removed", json={}).get_json()
        assert data == {"removed": 1, "kept": []}
        assert client.get("/api/admin/scrubber/status").get_json()["missing"] == 0
    finally:
        services.stop(timeout=5.0)
