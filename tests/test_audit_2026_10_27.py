"""The fourteenth audit (27 October 2026): what docs/development/audit-2026-10-26.md
found (A-01..A-05) and what a look at main since 3.3.0 found (B-01..B-03).

A-01  a deleted account's replica session became the next account's, an
      administrator's included, because the id is given again.
A-02  a new password or "Sign out everywhere" on the Mac left the Pi's
      sessions signed in.
A-03  an upload whose multipart part gives no length wrote past the free space
      kept for the index.
A-04  a setting changed on its own never reached the Pi.
A-05  ffmpeg for the trial and display copies was lowered by Python run
      between fork and exec, unsafe in a server with threads.
B-01  with Ninaivu.app moved while it ran, the console's restart (an import's
      too) failed on the Python and the folder that were no longer there.
B-02  a package installed after such a move is used only once Ninaivu is
      opened again; the page did not say so.
B-03  a full export made in Maintenance mode put the computer it was imported
      on into Maintenance too.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import time
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage

from ninaivu.replica.publish import Publisher
from ninaivu.replica.receive import carry_sessions
from ninaivu.server import auth
from ninaivu.storage import db


def _two_generations(tmp_path):
    old_path, new_path = tmp_path / "old.db", tmp_path / "new.db"
    old = db.init_db(old_path)
    auth.init_auth_schema(old)
    auth.create_user(old, "owner", "ownerpassword1", role=auth.ROLE_ADMIN)
    return old_path, new_path, old


def _next_generation(old, new_path):
    new = db.init_db(new_path)
    old.backup(new)
    auth.init_auth_schema(new)
    # A hand-over carries none of the Pi's sessions (replica/publish.slim).
    new.execute("DELETE FROM sessions")
    new.commit()
    return new


# --- A-01 ------------------------------------------------------------------------

def test_a_replaced_account_does_not_inherit_the_old_ones_replica_session(tmp_path):
    old_path, new_path, old = _two_generations(tmp_path)
    original = auth.create_user(old, "original", "originalpass1", role=auth.ROLE_FAMILY)
    token, _ = auth.start_session(old, original.id)
    new = _next_generation(old, new_path)
    auth.delete_user(new, original.id)
    replacement = auth.create_user(new, "replacement", "replacementpass1", role=auth.ROLE_ADMIN)
    assert original.id == replacement.id                # the id is given again
    db.close_all()
    carry_sessions(old_path, new_path)
    assert auth.session_user(db.connect(new_path), token) is None


def test_the_same_account_keeps_its_replica_session(tmp_path):
    old_path, new_path, old = _two_generations(tmp_path)
    maya = auth.create_user(old, "maya", "originalpass1", role=auth.ROLE_FAMILY)
    token, _ = auth.start_session(old, maya.id)
    _next_generation(old, new_path)
    db.close_all()
    assert carry_sessions(old_path, new_path) >= 1
    assert auth.session_user(db.connect(new_path), token).username == "maya"


# --- A-02 ------------------------------------------------------------------------

@pytest.mark.parametrize("ending", ["password", "everywhere", "turned off"])
def test_ending_an_accounts_sessions_on_the_mac_ends_them_on_the_pi(tmp_path, ending):
    old_path, new_path, old = _two_generations(tmp_path)
    maya = auth.create_user(old, "maya", "originalpass1", role=auth.ROLE_FAMILY)
    token, _ = auth.start_session(old, maya.id)
    time.sleep(0.01)
    new = _next_generation(old, new_path)
    if ending == "password":
        auth.set_password(new, maya.id, "replacementpass1")
    elif ending == "everywhere":
        auth.end_all_sessions(new, maya.id)
    else:
        auth.set_active(new, maya.id, False)
        auth.set_active(new, maya.id, True)
    db.close_all()
    carry_sessions(old_path, new_path)
    assert auth.session_user(db.connect(new_path), token) is None


def test_a_session_begun_on_the_pi_after_the_sign_out_is_kept(tmp_path):
    old_path, new_path, old = _two_generations(tmp_path)
    maya = auth.create_user(old, "maya", "originalpass1", role=auth.ROLE_FAMILY)
    new = _next_generation(old, new_path)
    auth.end_all_sessions(new, maya.id)
    time.sleep(0.01)
    token, _ = auth.start_session(old, maya.id)         # signed in again on the Pi
    db.close_all()
    carry_sessions(old_path, new_path)
    assert auth.session_user(db.connect(new_path), token).username == "maya"


def test_a_snapshot_from_an_older_mac_still_carries_sessions(tmp_path):
    """An older Mac's index has no signed_out_at: the account check alone."""
    old_path, new_path, old = _two_generations(tmp_path)
    maya = auth.create_user(old, "maya", "originalpass1", role=auth.ROLE_FAMILY)
    token, _ = auth.start_session(old, maya.id)
    new = _next_generation(old, new_path)
    new.execute("ALTER TABLE users DROP COLUMN signed_out_at")
    new.commit()
    db.close_all()
    carry_sessions(old_path, new_path)
    row = db.connect(new_path).execute("SELECT user_id FROM sessions WHERE token=?",
                                       (auth.session_key(token),)).fetchone()
    assert row is not None and row[0] == maya.id


# --- A-03 ------------------------------------------------------------------------

def _stage_with_free(tmp_path, monkeypatch, free_above_floor, size):
    from ninaivu.media import phone_backup, upload_review
    conn = db.init_db(tmp_path / "index.db")
    cfg = SimpleNamespace(state_dir=tmp_path, upload_quota_gb=0)
    upload = FileStorage(stream=io.BytesIO(b"x" * size), filename="clip.mp4")
    assert upload.content_length == 0                   # a browser's part says nothing
    written = []

    def usage(_):
        done = sum(p.stat().st_size for p in (tmp_path / "pending-uploads").rglob("*.mp4"))
        written.append(done)
        return shutil._ntuple_diskusage(10 ** 12, 0,
                                        phone_backup.KEEP_FREE_BYTES + free_above_floor - done)

    monkeypatch.setattr(upload_review.shutil, "disk_usage", usage)
    monkeypatch.setattr(upload_review, "SPACE_CHECK_EVERY", 4096)
    monkeypatch.setattr(upload_review, "_refuse_oversized", lambda _: None)
    monkeypatch.setattr(upload_review.scanner, "build_record",
                        lambda *a: {"kind": "video", "date_key": "", "size": size})
    return upload_review.stage(conn, cfg, upload, "clip.mp4", "/library", "", 1)


def test_an_upload_stops_before_the_free_space_kept_for_the_index(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not enough free space"):
        _stage_with_free(tmp_path, monkeypatch, free_above_floor=1024, size=4096)
    assert not list((tmp_path / "pending-uploads").rglob("*.mp4"))


def test_an_upload_stops_part_way_when_the_space_runs_out(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not enough free space"):
        _stage_with_free(tmp_path, monkeypatch, free_above_floor=3 * 1024 * 1024 + 10_000,
                         size=8 * 1024 * 1024)
    assert not list((tmp_path / "pending-uploads").rglob("*.mp4"))


def test_an_upload_with_room_is_written_whole(tmp_path, monkeypatch):
    result = _stage_with_free(tmp_path, monkeypatch, free_above_floor=10 ** 9, size=3 * 1024 * 1024)
    assert result["status"] == "pending"
    staged = list((tmp_path / "pending-uploads").rglob("*.mp4"))
    assert [p.stat().st_size for p in staged] == [3 * 1024 * 1024]


# --- A-04 ------------------------------------------------------------------------

def test_a_settings_change_alone_makes_a_hand_over_due(tmp_path):
    (tmp_path / "share").mkdir()
    cfg = SimpleNamespace(state_dir=tmp_path, db_path=tmp_path / "index.db",
                          replica_dir=str(tmp_path / "share"), replica_every_minutes=1,
                          open_browsing=True)
    cfg.db_path.write_bytes(b"index")
    publisher = Publisher(cfg)
    publisher._remember(folder=str(publisher.folder), at=time.time() - 120,
                        stamp=publisher._index_stamp())
    assert publisher.due() is False                     # nothing changed
    cfg.open_browsing = False
    assert publisher.due() is True


def test_the_settings_page_change_reaches_the_pi(as_admin, cfg, tmp_path):
    (tmp_path / "share").mkdir()
    cfg.replica_dir = str(tmp_path / "share")
    cfg.replica_every_minutes = 1
    publisher = Publisher(cfg)
    publisher._remember(folder=str(publisher.folder), at=time.time() - 120,
                        stamp=publisher._index_stamp())
    before = cfg.nsfw_filter
    response = as_admin.post("/api/settings", json={"nsfw_filter": not before})
    assert response.status_code == 200, response.get_json()
    assert publisher.due() is True


# --- A-05 ------------------------------------------------------------------------

def test_no_python_runs_between_fork_and_exec_for_ffmpeg():
    from pathlib import Path
    for name in ("ninaivu/media/video_trial.py", "ninaivu/mugil/display.py"):
        source = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        assert "preexec_fn=" not in source, name


def test_the_lowered_run_gives_what_subprocess_run_gave():
    from ninaivu.mugil import display
    done = display.run_lowered([sys.executable, "-c",
                                "import os, sys; print(os.nice(0) if hasattr(os, 'nice') else 10);"
                                "sys.stderr.write('said'); sys.exit(3)"], timeout=30)
    assert done.returncode == 3 and done.stderr == b"said"
    if sys.platform != "win32":
        assert int(done.stdout) >= 10                   # lowered from the parent


def test_the_lowered_run_stops_a_program_that_takes_too_long():
    import subprocess
    from ninaivu.mugil import display
    with pytest.raises(subprocess.TimeoutExpired):
        display.run_lowered([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)


# --- B-01 / B-02 ----------------------------------------------------------------

def test_a_restart_after_the_app_moved_runs_from_where_it_is_now(monkeypatch, tmp_path):
    from ninaivu.api import server_api
    from ninaivu.media import components
    moved = tmp_path / "Moved.app" / "python3.12"
    moved.parent.mkdir()
    moved.write_text("")
    started = {}

    def popen(command, **kwargs):
        started.update(command=command, cwd=kwargs["cwd"])
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(components.sys, "executable", str(tmp_path / "gone" / "python3"))
    monkeypatch.setattr(components, "_running_program", lambda: str(moved))
    monkeypatch.setattr(server_api, "ROOT", tmp_path / "gone")
    monkeypatch.setattr(server_api, "RESTART_LOG", tmp_path / "restart.log")
    monkeypatch.setattr(server_api, "_cfg", lambda: SimpleNamespace(state_dir=tmp_path))
    monkeypatch.setattr(server_api.subprocess, "Popen", popen)
    server_api._spawn_relauncher(None)
    assert started["command"][0] == str(moved)
    assert started["cwd"] == str(moved.parent)


def test_a_restart_with_the_app_gone_says_what_to_do(monkeypatch, tmp_path):
    from ninaivu.api import server_api
    from ninaivu.media import components
    monkeypatch.setattr(components.sys, "executable", str(tmp_path / "gone" / "python3"))
    monkeypatch.setattr(components, "_running_program", lambda: "")
    with pytest.raises(OSError, match="open it again"):
        server_api._spawn_relauncher(None)


def test_a_package_installed_after_a_move_says_to_open_ninaivu_again(monkeypatch, tmp_path):
    from ninaivu.media import components
    moved = tmp_path / "python3.12"
    moved.write_text("")
    monkeypatch.setattr(components.sys, "executable", str(tmp_path / "gone" / "python3"))
    monkeypatch.setattr(components, "_running_program", lambda: str(moved))
    entry = next(cid for cid, e in components.CATALOGUE.items()
                 if e["kind"] == "package" and not e.get("restart") and not e["present"]())
    assert components.describe(entry)["needs_restart"] is True


# --- B-03 ------------------------------------------------------------------------

def test_a_full_import_keeps_this_computers_maintenance_state(tmp_path, monkeypatch):
    from ninaivu.cli import backup_restore as restore
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir()
    there.mkdir()
    (here / "config.json").write_text(json.dumps({
        "home_name": "there", "workload_mode": "maintenance",
        "maintenance_before": {"mode": "overnight", "tuning_profile": "gentle", "since": 1}}))
    (there / "config.json").write_text(json.dumps({"workload_mode": "balanced"}))
    restore._keep_this_computer(there / "config.json", here / "config.json")
    settings = json.loads((here / "config.json").read_text())
    assert settings["home_name"] == "there"
    assert settings["workload_mode"] == "balanced" and "maintenance_before" not in settings
