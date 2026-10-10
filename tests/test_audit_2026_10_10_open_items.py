"""What the audit of 10 October 2026 and the flow pass left open.

Each test here failed before the change it is named for: the face grouping
that would not stop, the occasions rebuilt over the whole library on every
scan, the passes start-up forgot it owed, the compressions a restart lost,
the name announced in the way of the pages opening, the straightening
proposals that outlived an edit and the Stop that a queued survey could
lose, the portable build's sign-in value and folders, the upload quota, the
macOS package list without hashes, and identical drives taken for one.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ninaivu.storage import db

ROOT = Path(__file__).resolve().parents[1]


# --- face grouping stops when asked -------------------------------------------------------

def test_a_regroup_asked_to_stop_writes_nothing(monkeypatch):
    from ninaivu.media import faceindex

    saved = []
    monkeypatch.setattr(faceindex.db, "load_faces", lambda *a, **k: [])
    monkeypatch.setattr(faceindex.db, "save_cluster_keys",
                        lambda conn, pairs: saved.append(pairs))
    indexer = faceindex.FaceIndexer.__new__(faceindex.FaceIndexer)
    monkeypatch.setattr(indexer, "_load_people", lambda conn: [])

    assert indexer.regroup(None, ["/lib"], should_stop=lambda: True) == {"stopped": True}
    assert saved == []
    assert not faceindex._REGROUP_LOCK.locked()
    # Without a stop it goes through as before.
    assert "stopped" not in indexer.regroup(None, ["/lib"])
    assert len(saved) == 1


def test_grouping_faces_leaves_off_part_way_when_asked():
    np = pytest.importorskip("numpy")
    from ninaivu.media import facematch

    rng = np.random.default_rng(1)
    faces = [facematch.Candidate(face_id=i, vector=rng.normal(size=16).astype("float32"),
                                 quality=0.5, asset_id=i) for i in range(5000)]
    asked = []

    def stop() -> bool:
        asked.append(1)
        return len(asked) > 1

    clusters = facematch.cluster_faces(faces, should_stop=stop)
    assert sum(c.size for c in clusters) == 2000       # one stretch, then the stop
    assert sum(c.size for c in facematch.cluster_faces(faces)) == 5000


def test_the_scan_leaves_the_people_owed_when_a_regroup_stops(scanned, monkeypatch):
    cfg, conn, scanner = scanned

    class Indexer:
        engine = SimpleNamespace(available=True)

        def regroup(self, conn, root, should_stop=None):
            return {"stopped": True}

    monkeypatch.setattr(scanner, "_face_indexer", lambda: Indexer())
    monkeypatch.setattr(db, "count_assets_needing_faces", lambda *a: 0)
    db.set_meta(conn, f"faces_ungrouped:{cfg.active_root}", "1")
    conn.commit()
    scanner._find_faces(conn, cfg.active_root)
    assert db.get_meta(conn, f"faces_ungrouped:{cfg.active_root}") == "1"


# --- occasions: rebuilt only when something they read changed ------------------------------

def test_occasions_are_not_rebuilt_over_an_unchanged_library(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    root = cfg.active_root
    rebuilt = []
    real = db.rebuild_occasions
    monkeypatch.setattr(db, "rebuild_occasions",
                        lambda *a, **k: rebuilt.append(1) or real(*a, **k))

    # The scan in the fixture grouped them already.
    scanner._group_occasions(conn, root)
    scanner._group_occasions(conn, root)
    assert rebuilt == [], "nothing changed, and the whole library was grouped again"

    row = conn.execute("SELECT id, captured_at, mtime FROM assets WHERE root=? "
                       "ORDER BY id LIMIT 1", (root,)).fetchone()
    db.update_asset(conn, row["id"],
                    captured_at=float(row["captured_at"] or row["mtime"]) + 30 * 86400)
    scanner._group_occasions(conn, root)
    assert len(rebuilt) == 1, "a photograph moved, and its occasions were not rebuilt"

    cfg.occasion_min_items = int(cfg.occasion_min_items) + 1
    scanner._group_occasions(conn, root)
    assert len(rebuilt) == 2, "the settings changed, and the occasions were not rebuilt"

    conn.execute("DELETE FROM occasions WHERE root=?", (root,))
    conn.commit()
    scanner._group_occasions(conn, root)
    assert len(rebuilt) == 3, "the occasions were changed elsewhere, and not rebuilt"


def test_two_photographs_swapping_their_times_changes_the_fingerprint(scanned):
    cfg, conn, _ = scanned
    root = cfg.active_root
    a, b = conn.execute("SELECT id, captured_at, mtime FROM assets WHERE root=? "
                        "ORDER BY id LIMIT 2", (root,)).fetchall()
    when_a = 1_500_000_000.0
    when_b = 1_600_000_000.0
    db.update_asset(conn, a["id"], captured_at=when_a)
    db.update_asset(conn, b["id"], captured_at=when_b)
    before = db.occasion_inputs(conn, root)
    db.update_asset(conn, a["id"], captured_at=when_b)
    db.update_asset(conn, b["id"], captured_at=when_a)
    assert db.occasion_inputs(conn, root) != before
    db.update_asset(conn, a["id"], city="Madurai")
    after_town = db.occasion_inputs(conn, root)
    db.update_asset(conn, a["id"], city="Chennai")
    assert db.occasion_inputs(conn, root) != after_town


# --- start-up remembers the passes it owes --------------------------------------------------

def test_a_pass_left_waiting_is_still_owed_at_start_up(scanned):
    from ninaivu.media import scanner as scanner_mod

    cfg, conn, scanner = scanned
    root = cfg.active_root
    assert scanner_mod.still_owed(conn, root, "text"), "nothing has been read yet"

    # A pass that went to its end leaves nothing owed, even with photographs
    # it could not read (no thumbnail): those are written down as left.
    scanner._leave(conn, root, "text")
    assert not scanner_mod.still_owed(conn, root, "text")

    # Something new to read is owed again.
    conn.execute("UPDATE assets SET ocr_version=99 WHERE root=?", (root,))
    conn.commit()
    scanner._leave(conn, root, "text")
    assert not scanner_mod.still_owed(conn, root, "text")
    one = conn.execute("SELECT id FROM assets WHERE root=? AND kind='picture' "
                       "AND thumb IS NOT NULL ORDER BY id LIMIT 1", (root,)).fetchone()["id"]
    conn.execute("UPDATE assets SET ocr_version=0 WHERE id=?", (one,))
    conn.commit()
    assert scanner_mod.still_owed(conn, root, "text")


def test_start_up_carries_on_owed_passes_without_walking(scanned, monkeypatch):
    from ninaivu import Services

    cfg, conn, _ = scanned
    cfg.ocr_enabled = True
    services = Services.__new__(Services)
    services.cfg = cfg
    started = []
    services.scanner = SimpleNamespace(
        start=lambda roots, **kw: started.append((list(map(str, roots)), kw)),
        watch=lambda: started.append("watch"))
    monkeypatch.setattr(Services, "_scan_worth_it", lambda self, root: False)

    due = services._scan_the_libraries()
    assert due == [cfg.active_root]
    (roots, kw), = started
    assert roots == [cfg.active_root]
    assert kw["scopes"] == {Path(cfg.active_root): frozenset()}, "analysis only, no walk"

    # Off, nothing is owed and nothing starts but the watcher.
    cfg.ocr_enabled = False
    started.clear()
    assert services._scan_the_libraries() == []
    assert started == ["watch"]


# --- compressions carry on after a restart --------------------------------------------------

@pytest.fixture()
def compress_queue(tmp_path):
    from ninaivu.media import video_compress as vc

    vc._reset_for_tests()
    yield vc, tmp_path / "compress-queue.json"
    vc._reset_for_tests()


def test_a_compression_waiting_when_ninaivu_stopped_is_queued_again(compress_queue):
    vc, path = compress_queue
    release = threading.Event()

    def slow(job, report, cancelled):
        release.wait(10)
        return {"ok": True}

    vc.resume(path, lambda plan: None)            # the queue is kept in *path* now
    first = vc.start(1, "copy", "a.mov", slow, plan={"asset_id": 1, "mode": "copy",
                                                     "user_id": 7})
    vc.start(2, "replace", "b.mov", slow, plan={"asset_id": 2, "mode": "replace",
                                                "user_id": 7})
    saved = json.loads(path.read_text())
    assert [(p["asset_id"], p["mode"], p["user_id"]) for p in saved] == [
        (1, "copy", 7), (2, "replace", 7)]

    # The restart: the queue in memory is gone, the file is not.
    with vc._lock:
        vc._jobs.clear()
        vc._queue_file = None
    release.set()
    made = []

    def make(plan):
        made.append(plan["asset_id"])
        return f"video {plan['asset_id']}", lambda job, report, cancelled: {"ok": True}

    queued = vc.resume(path, make)
    assert made == [1, 2] and [j["asset_id"] for j in queued] == [1, 2]
    for job in queued:
        assert vc.wait(job["id"], 10)["state"] == "done"
    assert not path.exists(), "nothing is left to carry on"
    assert first["id"] not in {j["id"] for j in queued}


def test_an_old_or_impossible_compression_is_not_carried_on(compress_queue):
    vc, path = compress_queue
    now = time.time()
    path.write_text(json.dumps([
        {"asset_id": 1, "mode": "copy", "queued_at": now - 3 * 86400},
        {"asset_id": 2, "mode": "replace", "queued_at": now - 60},
        {"asset_id": 3, "mode": "shred", "queued_at": now - 60},
    ]))
    asked = []

    def make(plan):
        asked.append(plan["asset_id"])
        return None                               # deleted since, say

    assert vc.resume(path, make, now=now) == []
    assert asked == [2]
    assert not path.exists()


def test_a_stopped_compression_is_not_carried_on(compress_queue):
    vc, path = compress_queue
    gate = threading.Event()
    vc.resume(path, lambda plan: None)
    job = vc.start(5, "copy", "c.mov", lambda j, r, c: gate.wait(10) and {},
                   plan={"asset_id": 5, "mode": "copy", "user_id": 1})
    assert path.exists()
    vc.cancel(job["id"])
    assert not path.exists()
    gate.set()


def test_start_up_asks_the_compress_questions_again(scanned, monkeypatch):
    from ninaivu.api import admin_api
    from ninaivu.media import video_compress as vc

    cfg, conn, _ = scanned
    vc._reset_for_tests()
    video = conn.execute("SELECT id FROM assets WHERE kind='video' LIMIT 1").fetchone()
    photo = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    queue = Path(cfg.state_dir) / "compress-queue.json"
    plans = [{"asset_id": photo["id"], "mode": "copy", "queued_at": time.time()}]
    if video is not None:
        plans.append({"asset_id": video["id"], "mode": "copy", "queued_at": time.time()})
    queue.write_text(json.dumps(plans))
    monkeypatch.setattr(vc, "unavailable_reason", lambda: None)
    monkeypatch.setattr(vc, "start", lambda asset_id, mode, name, work, **kw: {
        "asset_id": asset_id})
    queued = admin_api.resume_compressions(cfg)
    assert photo["id"] not in [j["asset_id"] for j in queued], "a photograph is no video"
    vc._reset_for_tests()


# --- the name is announced beside start-up, not before it -------------------------------------

def test_announcing_the_name_does_not_hold_start_up(monkeypatch):
    pytest.importorskip("zeroconf")
    from ninaivu.utils import discovery

    gate = threading.Event()
    registered = []

    def slow(announcement, *args):
        gate.wait(10)
        registered.append(announcement)
        return True

    monkeypatch.setattr(discovery, "_register", slow)
    started = time.monotonic()
    announcement = discovery.advertise("ninaivu-quick", {"family": 5000},
                                       ["192.168.1.24"], admin_name="ninaivu-quick-admin",
                                       wait=False)
    assert time.monotonic() - started < 2
    assert announcement is not None
    assert announcement.hostnames == {"family": "ninaivu-quick.local",
                                      "admin": "ninaivu-quick-admin.local"}
    gate.set()
    announcement.close()
    assert registered == [announcement], "close waited for the announcement to finish"


def test_start_up_announces_without_waiting():
    text = (ROOT / "ninaivu" / "__main__.py").read_text(encoding="utf-8")
    call = text[text.index("discovery.advertise("):]
    assert "wait=False" in call[:call.index(")\n")]


# --- straightening: proposals that no longer describe their photograph ------------------------

@pytest.fixture()
def proposals(scanned):
    from ninaivu.media import straighten

    cfg, conn, _ = scanned
    straighten.init_schema(conn)
    rows = conn.execute("SELECT id, indexed_at FROM assets WHERE kind='picture' "
                        "ORDER BY id LIMIT 3").fetchall()
    for row in rows:
        conn.execute("UPDATE assets SET rotation=0, rot_source='none' WHERE id=?",
                     (row["id"],))
        conn.execute(
            "INSERT INTO orientation_proposals(asset_id, rotation, confidence, "
            "prev_rotation, prev_rot_source, status, surveyed_at, indexed_at) "
            "VALUES(?, 90, 0.99, 0, 'none', 'pending', ?, ?)",
            (row["id"], time.time(), row["indexed_at"]))
    conn.commit()
    return straighten, cfg, conn, [r["id"] for r in rows]


def test_a_photograph_edited_since_it_was_looked_at_is_not_turned_by_the_old_verdict(proposals):
    straighten, cfg, conn, (kept, edited, turned) = proposals
    db.update_asset(conn, edited, indexed_at=time.time() + 100)      # indexed again
    db.update_asset(conn, turned, rotation=180, rot_source="user")   # turned by hand

    assert straighten.drop_outdated(conn) == 2
    left = {r[0] for r in conn.execute(
        "SELECT asset_id FROM orientation_proposals WHERE status='pending'")}
    assert left == {kept}


def test_a_proposal_from_before_the_indexing_was_recorded_is_judged_by_its_turn(proposals):
    straighten, cfg, conn, (one, two, _) = proposals
    conn.execute("UPDATE orientation_proposals SET indexed_at=NULL")
    conn.commit()
    db.update_asset(conn, one, indexed_at=time.time() + 100)
    db.update_asset(conn, two, rotation=90, rot_source="user")
    straighten.drop_outdated(conn)
    left = {r[0] for r in conn.execute(
        "SELECT asset_id FROM orientation_proposals WHERE status='pending'")}
    assert one in left and two not in left


def test_a_survey_queued_behind_the_scan_does_not_start_after_stop(cfg):
    from ninaivu.media import straighten

    st = straighten.Straightener(cfg)
    queued = st._stops
    st.stop()
    started = []
    st._survey = lambda *a, **k: started.append(1)
    assert st.survey([cfg.active_root], queued=queued) is False
    assert started == []
    assert st.survey([cfg.active_root], queued=st._stops) is True
    st.stop(join=True)


def test_stop_pressed_as_a_survey_starts_is_not_carried_on(cfg, monkeypatch):
    from ninaivu.media import straighten
    from ninaivu.storage import resume

    conn = db.init_db(cfg.db_path)
    st = straighten.Straightener(cfg)
    gate = threading.Event()

    def survey_all(conn, roots, limit, rescan, began=None):
        gate.wait(10)

    monkeypatch.setattr(st, "_survey_all", survey_all)
    assert st.survey([cfg.active_root])
    st.stop(forget=True)          # pressed Stop: what the route does
    resume.done(conn, straighten.RESUME_NAME)
    gate.set()
    st._thread.join(10)
    assert straighten.RESUME_NAME not in resume.wanted(conn)


def test_auto_apply_finds_this_runs_proposals_after_the_clock_steps_back(cfg, monkeypatch):
    from ninaivu.media import straighten

    seen = []
    st = straighten.Straightener(cfg)

    def judge_in_order(conn, ahead, top_up, judge, seen_ids, judged, pending, stale,
                       requires_face, now):
        seen.append(now)

    monkeypatch.setattr(st, "_judge_in_order", judge_in_order)
    monkeypatch.setattr(st, "_candidates", lambda conn, roots, limit=None: [])
    monkeypatch.setattr("ninaivu.media.orientnet.available", lambda: True)
    cfg.straighten_requires_face = False
    conn = db.init_db(cfg.db_path)
    straighten.init_schema(conn)
    st._survey_all(conn, [cfg.active_root], None, False, began=1234.0)
    assert seen == [1234.0]


# --- the portable build ---------------------------------------------------------------------

def test_the_launcher_tells_ninaivu_which_program_it_is():
    source = (ROOT / "installers" / "windows" / "portable" / "Ninaivu.cs").read_text()
    assert 'EnvironmentVariables["NINAIVU_LAUNCHER"]' in source


def test_a_renamed_launcher_is_what_starts_at_sign_in(tmp_path, monkeypatch):
    from ninaivu.desktop import autostart

    (tmp_path / "app").mkdir()
    renamed = tmp_path / "Family photos.exe"
    renamed.write_bytes(b"")
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    monkeypatch.setenv("NINAIVU_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("NINAIVU_LAUNCHER", str(renamed))
    assert autostart.command(tmp_path / "app", "win32") == [str(renamed), "--autostart"]


def test_a_portable_copy_with_no_launcher_is_refused_not_started_bare(tmp_path, monkeypatch):
    from ninaivu.desktop import autostart

    (tmp_path / "app").mkdir()
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    monkeypatch.setenv("NINAIVU_HOME", str(tmp_path / "app"))
    monkeypatch.delenv("NINAIVU_LAUNCHER", raising=False)
    with pytest.raises(RuntimeError):
        autostart.command(tmp_path / "app", "win32")


def test_another_portable_copys_sign_in_start_is_not_this_ones(tmp_path, monkeypatch):
    from ninaivu.desktop import autostart

    (tmp_path / "app").mkdir()
    (tmp_path / "Ninaivu.exe").write_bytes(b"")
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    monkeypatch.setenv("NINAIVU_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("NINAIVU_LAUNCHER", str(tmp_path / "Ninaivu.exe"))
    ours = subprocess.list2cmdline([str(tmp_path / "Ninaivu.exe"), "--autostart"])
    monkeypatch.setattr(autostart, "_stored_command", lambda: ours)
    assert autostart.enabled(platform="win32")
    monkeypatch.setattr(autostart, "_stored_command",
                        lambda: r'"F:\Ninaivu\Ninaivu.exe" --autostart')
    assert not autostart.enabled(platform="win32")
    monkeypatch.setattr(autostart, "_stored_command", lambda: None)
    assert not autostart.enabled(platform="win32")


@pytest.fixture()
def windows(monkeypatch, tmp_path):
    from ninaivu.server import config as config_mod

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(config_mod, "_PRIVATE_ON_WINDOWS", set())
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Users" / "amma"))
    monkeypatch.setenv("USERNAME", "amma")
    monkeypatch.setenv("USERDOMAIN", "HOMEPC")
    return SimpleNamespace(calls=calls, config=config_mod)


def test_the_program_folder_is_made_private_once_for_good(windows, tmp_path, monkeypatch):
    folder = tmp_path / "E" / "Ninaivu" / "app"
    folder.mkdir(parents=True)
    windows.config.private_on_windows(folder, remember=True)
    assert len(windows.calls) == 1
    assert (folder / windows.config.PRIVATE_MARK).read_text() == "HOMEPC\\amma"
    # The next start, a new process: nothing to do.
    monkeypatch.setattr(windows.config, "_PRIVATE_ON_WINDOWS", set())
    windows.config.private_on_windows(folder, remember=True)
    assert len(windows.calls) == 1
    # Another account starting the same copy makes it its own.
    monkeypatch.setattr(windows.config, "_PRIVATE_ON_WINDOWS", set())
    monkeypatch.setenv("USERNAME", "appa")
    windows.config.private_on_windows(folder, remember=True)
    assert len(windows.calls) == 2


def test_the_portable_logs_and_program_are_made_private(tmp_path, monkeypatch):
    from ninaivu.server import config as config_mod
    from ninaivu.server.config import Config

    portable = tmp_path / "E" / "Ninaivu"
    (portable / "app").mkdir(parents=True)
    (portable / "logs").mkdir()
    monkeypatch.setenv("NINAIVU_PORTABLE", "1")
    monkeypatch.setenv("NINAIVU_HOME", str(portable / "app"))
    monkeypatch.setenv("NINAIVU_LOG_DIR", str(portable / "logs"))
    made = []
    monkeypatch.setattr(config_mod, "private_on_windows",
                        lambda folder, remember=False: made.append((str(folder), remember)))
    monkeypatch.setattr(threading, "Thread", lambda target, args, kwargs, **_: SimpleNamespace(
        start=lambda: target(*args, **kwargs)))
    cfg = Config()
    cfg.state_dir = portable / "app" / "data"
    cfg.ensure_dirs()
    assert (str(portable / "logs"), False) in made
    assert (str(portable / "app"), True) in made


def test_the_portable_readme_says_what_reaches_outside_the_folder():
    readme = (ROOT / "installers" / "windows" / "portable" / "README-PORTABLE.txt").read_text()
    assert "Trusted Root Certification Authorities" in readme
    assert "winget" in readme
    assert "%LOCALAPPDATA%\\Ninaivu\\control" in readme
    assert "app\\.ninaivu-control\\settings.json" in readme


# --- one person cannot fill the disk with uploads nobody approved ------------------------------

def _jpeg() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), "green").save(stream, "JPEG")
    return stream.getvalue()


@pytest.fixture()
def homes(scanned):
    from conftest import login
    from ninaivu import build_services, create_home_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    admin = auth.bootstrap_admin(conn, "dad", "correct horse battery", "Dad")
    auth.create_user(conn, "maya", "maya's long password", display_name="Maya",
                     role="family", created_by=admin.id)
    services = build_services(cfg)
    services.scanner.stop(join=True)
    home = create_home_app(services)

    yield services.cfg, login(home.test_client(), "maya", "maya's long password"), login(
        home.test_client(), "dad", "correct horse battery")
    services.stop()


def test_uploads_past_the_quota_are_refused_for_a_family_member(homes, monkeypatch):
    from ninaivu.media import upload_review

    cfg, family, admin = homes
    cfg.upload_quota_gb = 1
    send = lambda client: client.post(  # noqa: E731
        "/api/upload", data={"file": (io.BytesIO(_jpeg()), "one.jpg")}).get_json()
    assert send(family)["total"] == 1
    monkeypatch.setattr(upload_review, "waiting_bytes", lambda conn, user: 1024 ** 3)
    refused = send(family)
    assert refused["total"] == 0 and "waiting" in refused["errors"][0]["error"]
    assert send(admin)["total"] == 1, "an administrator is not held to it"
    cfg.upload_quota_gb = 0
    assert send(family)["total"] == 1, "zero means no limit"


def test_what_is_waiting_is_counted_per_person(scanned):
    from ninaivu.media import upload_review
    from ninaivu.server import auth

    _, conn, _ = scanned
    auth.init_auth_schema(conn)
    for user, size, status in ((1, 100, "pending"), (1, 50, "pending"),
                               (1, 999, "approved"), (2, 7, "pending")):
        conn.execute("INSERT INTO pending_uploads(storage_key, filename, root, scope, "
                     "uploaded_by, uploaded_at, record, status) VALUES(?,?,?,?,?,?,?,?)",
                     (os.urandom(8).hex(), "x.jpg", "/lib", "", user, time.time(),
                      json.dumps({"size": size}), status))
    conn.commit()
    assert upload_review.waiting_bytes(conn, 1) == 150
    assert upload_review.waiting_bytes(conn, 2) == 7


def test_the_quota_is_a_setting_on_the_console():
    from ninaivu.server import settings_groups

    assert any("upload_quota_gb" in names for names in settings_groups.GROUPS.values())


# --- the Mac's package list carries hashes ---------------------------------------------------

def test_the_mac_build_lists_what_it_carries_with_hashes():
    script = (ROOT / "installers" / "macos" / "build.sh").read_text()
    assert "-m pip freeze" not in script
    assert "shasum -a 256" in script and "-packages.txt" in script
    assert '--no-index --find-links "$deps"' in script


# --- identical drives are two drives ----------------------------------------------------------

def test_two_identical_sticks_are_remembered_apart():
    from ninaivu.utils.drives import Drive

    one = Drive("a", "E:\\", "USB DRIVE", 32_000_000_000, 0, volume="1A2B3C4D")
    other = Drive("b", "F:\\", "USB DRIVE", 32_000_000_000, 0, volume="5E6F7A8B")
    unknown = Drive("c", "G:\\", "USB DRIVE", 32_000_000_000, 0)
    assert one.remember_key != other.remember_key
    assert unknown.remember_key == "drive|USB DRIVE|32000000000"


def test_linux_finds_a_volumes_uuid_by_its_device(tmp_path, monkeypatch):
    from ninaivu.utils import drives

    folder = tmp_path / "by-uuid"
    folder.mkdir()
    (folder / "1234-ABCD").write_text("")
    (folder / "5678-EF01").write_text("")
    monkeypatch.setattr(drives, "BY_UUID", str(folder))
    real_stat = os.stat

    def stat(path, *a, **k):
        name = os.path.basename(path)
        if name in ("1234-ABCD", "5678-EF01"):
            return SimpleNamespace(st_rdev=2049 if name == "5678-EF01" else 2050)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(drives.os, "stat", stat)
    assert drives._linux_uuid(2049) == "5678-EF01"
    assert drives._linux_uuid(99) == ""
