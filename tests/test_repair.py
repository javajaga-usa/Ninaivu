"""Damaged files put back from a copy whose bytes match, and the check on a schedule."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

import pytest

from ninaivu.api import admin_api
from ninaivu.cloud import store
from ninaivu.storage import db
from ninaivu.storage.mirror import Mirror
from ninaivu.storage.repair import Repairer, candidates


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rot(path: Path) -> None:
    """Change bytes and nothing else: what decay on a disk looks like."""
    before = path.stat()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    os.utime(path, (before.st_atime, before.st_mtime))


@pytest.fixture()
def lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    cfg.mirror_dir = str(tmp_path / "mirror")
    Path(cfg.mirror_dir).mkdir()
    mirror = Mirror(cfg, lambda: db.connect(cfg.db_path))
    root = cfg.active_root
    row = conn.execute("SELECT id, rel_path FROM assets WHERE filename='shot1.jpg'").fetchone()
    path = Path(root) / row["rel_path"]
    admin_api._run_scrubber(cfg.db_path)            # fingerprints first
    return {"cfg": cfg, "conn": conn, "mirror": mirror, "root": root, "id": row["id"],
            "rel": row["rel_path"], "path": path, "good": sha(path)}


def copy_to_mirror(lib, damaged=False):
    dest = lib["mirror"]._on_disk(Path(lib["cfg"].mirror_dir), lib["root"], lib["rel"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(lib["path"].read_bytes())
    if damaged:
        rot(dest)
    return dest


def repair(lib, **kw):
    repairer = Repairer(lib["cfg"], lambda: db.connect(lib["cfg"].db_path),
                        mirror=lib["mirror"], **kw)
    repairer.start()
    repairer.join(30)
    return repairer


def latest(conn, asset_id):
    return conn.execute("SELECT status FROM bitrot_records WHERE asset_id=? ORDER BY id DESC "
                        "LIMIT 1", (asset_id,)).fetchone()["status"]


def test_a_rotted_file_comes_back_from_the_second_copy(lib):
    copy_to_mirror(lib)
    mtime = lib["path"].stat().st_mtime
    rot(lib["path"])
    admin_api._run_scrubber(lib["cfg"].db_path)
    assert latest(lib["conn"], lib["id"]) == "corrupt"
    assert [c["asset_id"] for c in candidates(lib["conn"])] == [lib["id"]]

    repairer = repair(lib)
    assert sha(lib["path"]) == lib["good"]
    assert abs(lib["path"].stat().st_mtime - mtime) < 0.001, "the file keeps its time"
    assert latest(lib["conn"], lib["id"]) == "repaired"
    kept = list(repairer.damaged_folder().rglob("shot1.jpg"))
    assert len(kept) == 1 and sha(kept[0]) != lib["good"], "the damaged one is kept"
    assert repairer.status()["repaired"] == 1 and repairer.status()["waiting"] == 0
    # The next check sees a good file, not an edit.
    admin_api._run_scrubber(lib["cfg"].db_path)
    assert latest(lib["conn"], lib["id"]) == "verified"


def test_a_copy_that_is_damaged_too_is_never_used(lib):
    copy_to_mirror(lib, damaged=True)
    rot(lib["path"])
    damaged = lib["path"].read_bytes()
    admin_api._run_scrubber(lib["cfg"].db_path)
    repairer = repair(lib)
    assert lib["path"].read_bytes() == damaged, "nothing was changed"
    problems = repairer.status()["problems"]
    assert problems and "different too" in problems[0]["why"]
    assert latest(lib["conn"], lib["id"]) == "corrupt"


def test_a_missing_file_is_put_back_only_while_its_disk_is_there(lib):
    copy_to_mirror(lib)
    lib["path"].unlink()
    admin_api._run_scrubber(lib["cfg"].db_path)
    assert latest(lib["conn"], lib["id"]) == "missing"
    repair(lib)
    assert sha(lib["path"]) == lib["good"]


def test_a_library_that_is_not_plugged_in_is_not_repaired(lib, tmp_path):
    copy_to_mirror(lib)
    lib["path"].unlink()
    admin_api._run_scrubber(lib["cfg"].db_path)
    moved = tmp_path / "unplugged"
    os.rename(lib["root"], moved)
    try:
        repairer = repair(lib)
        assert "plugged in" in repairer.status()["problems"][0]["why"]
        assert not Path(lib["root"]).exists(), "no folder was made where the disk was"
    finally:
        os.rename(moved, lib["root"])


class FakeClient:
    def __init__(self, data: bytes):
        self.data = data

    def file_info(self, remote_id):
        return {"size": len(self.data)}

    def download_range(self, remote_id, start, end):
        return self.data[start:end + 1]


class FakeCloud:
    def __init__(self, data):
        self.creds = type("C", (), {"connected": True})()
        self._client = FakeClient(data)

    def client(self):
        return self._client

    def restore_key(self, items):
        return None


def test_without_a_second_copy_it_comes_back_from_drive(lib):
    good = lib["path"].read_bytes()
    conn = lib["conn"]
    store.init_schema(conn)
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, filename, size, digest, state, "
                 "remote_id) VALUES(?, ?, 'shot1.jpg', ?, ?, 'done', 'r1')",
                 (lib["root"], lib["rel"], len(good), lib["good"]))
    conn.commit()
    rot(lib["path"])
    admin_api._run_scrubber(lib["cfg"].db_path)
    repairer = repair(lib, cloud=FakeCloud(good))
    assert sha(lib["path"]) == lib["good"], repairer.status()
    assert not list((Path(lib["cfg"].state_dir) / "repair").glob("from-drive-*")), "staging tidied"


def test_the_check_runs_by_itself_at_night_when_due(lib):
    started = []
    night = datetime(2026, 10, 3, 2, 30).timestamp()
    noon = datetime(2026, 10, 3, 12, 0).timestamp()

    def make(clock, days=30):
        lib["cfg"].scrub_every_days = days
        return Repairer(lib["cfg"], lambda: db.connect(lib["cfg"].db_path), clock=lambda: clock,
                        start_check=lambda on_done=None: started.append(on_done) or True)

    assert make(noon + 40 * 86400).tick() is False, "not in the middle of the day"
    assert make(night + 40 * 86400).tick() is True
    lib["conn"].execute("UPDATE bitrot_records SET checked_at=?", (night - 86400,))
    lib["conn"].commit()
    assert make(night).check_due() is False, "checked yesterday"
    assert make(night + 40 * 86400, days=0).tick() is False, "0 means never"
    assert started and callable(started[0])


def test_after_a_check_what_it_found_is_repaired(lib):
    copy_to_mirror(lib)
    rot(lib["path"])
    admin_api._run_scrubber(lib["cfg"].db_path)
    repairer = Repairer(lib["cfg"], lambda: db.connect(lib["cfg"].db_path), mirror=lib["mirror"])
    repairer.after_check()
    repairer.join(30)
    assert sha(lib["path"]) == lib["good"]
    lib["cfg"].scrub_repair = False
    rot(lib["path"])
    admin_api._run_scrubber(lib["cfg"].db_path)
    repairer.after_check()
    repairer.join(30)
    assert sha(lib["path"]) != lib["good"], "not when automatic repair is off"


def test_the_console_can_see_and_set_it(scanned):
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        client = login(create_admin_app(services).test_client(), *ADMIN)
        data = client.get("/api/admin/repair").get_json()
        assert data["every_days"] == 30 and data["automatic"] is True and data["waiting"] == 0
        data = client.post("/api/admin/repair/settings",
                           json={"every_days": 7, "automatic": False}).get_json()
        assert data["every_days"] == 7 and data["automatic"] is False
        assert client.post("/api/admin/repair/settings", json={"every_days": 999}).status_code == 400
        assert client.post("/api/admin/repair", json={"ids": "x"}).status_code == 400
    finally:
        services.stop(timeout=5.0)
