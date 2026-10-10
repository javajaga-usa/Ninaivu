"""The audit of 10 October 2026, second pass: sign-in pauses and resumed
compressions. (The phone inbox's tests are in test_phone_inbox.py, the TV
album's in test_tv_album.py, Keepsake's in test_audit_2026_10_10b_keepsake.py.)"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ninaivu.api import accounts_api, admin_api
from ninaivu.media import video_compress as vc
from ninaivu.server import auth


def test_a_flood_of_made_up_names_does_not_displace_a_real_profiles_pause(monkeypatch):
    now = time.time()
    monkeypatch.setattr(accounts_api, "_LOCKOUTS", {})
    monkeypatch.setattr(accounts_api, "_ATTEMPTS", {})
    monkeypatch.setattr(accounts_api, "_MAX_KEYS", 100)
    monkeypatch.setattr(accounts_api, "_last_sweep", 0.0)
    # A real profile, paused three times; its pause ends before the flood's.
    accounts_api._LOCKOUTS["*|user:dad"] = (3, now + 60)
    for i in range(100):
        accounts_api._LOCKOUTS[f"*|user:made-up-{i}"] = (1, now + 1800)
    accounts_api._sweep(now)
    assert accounts_api._LOCKOUTS.get("*|user:dad") == (3, now + 60)


def _video(cfg, conn, name="clip.mp4"):
    root = Path(cfg.active_root)
    (root / "clips").mkdir(exist_ok=True)
    path = root / "clips" / name
    path.write_bytes(b"0" * 4096)
    cur = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, mtime, "
        "date_key, duration) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(root), f"clips/{name}", name, "clips", ".mp4", "video", 4096,
         path.stat().st_mtime, time.strftime("%Y-%m-%d"), 10.0))
    conn.commit()
    return int(cur.lastrowid), path


def _resume(cfg, monkeypatch, plans):
    vc._reset_for_tests()
    (Path(cfg.state_dir) / "compress-queue.json").write_text(json.dumps(plans))
    monkeypatch.setattr(vc, "unavailable_reason", lambda: None)
    monkeypatch.setattr(vc, "start", lambda asset_id, mode, name, work, **kw: {
        "asset_id": asset_id})
    monkeypatch.setattr(admin_api, "_damaged_video", lambda row: False)
    try:
        return [j["asset_id"] for j in admin_api.resume_compressions(cfg)]
    finally:
        vc._reset_for_tests()


def test_a_replace_is_carried_on_only_for_an_administrator_and_the_same_file(
        scanned, monkeypatch):
    cfg, conn, _ = scanned
    admin = auth.bootstrap_admin(conn, "dad", "correct horse battery", "Dad")
    family = auth.create_user(conn, "maya", "correct horse battery 2",
                              role=auth.ROLE_FAMILY, created_by=admin.id)
    video, path = _video(cfg, conn)
    stat = path.stat()
    plan = {"asset_id": video, "mode": "replace", "queued_at": time.time(),
            "size": stat.st_size, "mtime": stat.st_mtime}
    assert _resume(cfg, monkeypatch, [dict(plan, user_id=admin.id)]) == [video]
    # Asked for by somebody who is no longer an administrator.
    assert _resume(cfg, monkeypatch, [dict(plan, user_id=family.id)]) == []
    assert _resume(cfg, monkeypatch, [dict(plan, user_id=9999)]) == []
    # The file is not the one it was when the Replace was agreed to.
    path.write_bytes(b"1" * 5000)
    assert _resume(cfg, monkeypatch, [dict(plan, user_id=admin.id)]) == []


def test_a_compression_remembers_the_file_it_was_asked_for():
    text = (Path(__file__).resolve().parents[1] / "ninaivu" / "api" / "admin_api.py").read_text()
    assert "**_file_stamp(row)" in text
    job = vc.start(1, "copy", "x.mp4", lambda j, r, c: {},
                   plan={"asset_id": 1, "mode": "copy", "user_id": 1, "size": 5, "mtime": 2.0})
    try:
        assert vc._jobs[job["id"]]["plan"] == {"asset_id": 1, "mode": "copy", "user_id": 1,
                                               "size": 5, "mtime": 2.0}
    finally:
        vc._reset_for_tests()
