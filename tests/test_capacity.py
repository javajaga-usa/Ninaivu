"""The console's Performance page: what this computer can do for Ninaivu, and
what would help it do more.

The advice is a plain function of measured facts, so every rule is checked
here on every system — a Mac's rules on Windows, and the other way round. The
facts below are the ones the page was written from: an Apple M6 with its image
model on the processor, and its library on a drive macOS mounts read-only.
"""
import copy
import sqlite3

import pytest

from ninaivu.server import capacity

GB = 1024 ** 3

MAC = {
    "platform": "darwin",
    "machine": {"processor": "Apple M6", "logical_cores": 12, "memory_bytes": 16 * GB,
                "core_kinds": [{"name": "Super", "cores": 2}]},
    "graphics": {"available": "mps", "name": "Apple M6 graphics", "ai_device": "cpu",
                 "in_use": None},
    "load": {"memory_percent": 58.0, "memory_available_bytes": 6 * GB,
             "swap_used_bytes": 0, "ninaivu_memory_bytes": GB // 2, "battery": None},
    "storage": [
        {"role": "library", "path": "/Volumes/Red/MasterArchive", "present": True,
         "filesystem": "ntfs", "read_only": True, "bus": "USB", "solid_state": None,
         "internal": False, "free_bytes": 1600 * GB, "total_bytes": 5000 * GB},
        {"role": "index", "path": "/Users/j/.ninaivu", "present": True,
         "filesystem": "apfs", "read_only": False, "bus": "Apple Fabric",
         "solid_state": True, "internal": True, "free_bytes": 100 * GB,
         "total_bytes": 245 * GB},
    ],
    "scans": {"/Volumes/Red/MasterArchive": {"seconds": 220.4, "files": 210157,
                                             "files_per_second": 954}},
    "backlog": {"analysis": 0, "faces": 0},
    "ninaivu": {"mode": "performance", "workers": 12, "schedule": "overnight",
               "night": ["23:00", "06:00"], "ai_enabled": True, "ai_gpu": True,
               "ai_semantic": True, "ffmpeg": True},
}


def facts(**changes):
    """MAC, with dotted keys changed: facts(**{"ninaivu.ai_gpu": False})."""
    out = copy.deepcopy(MAC)
    for dotted, value in changes.items():
        target = out
        *path, last = dotted.split(".")
        for key in path:
            target = target[int(key)] if isinstance(target, list) else target[key]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value
    return out


def advice(given, key):
    return next((a for a in capacity.recommend(given) if a["id"].split(":")[0] == key), None)


# --- the graphics processor --------------------------------------------------

def test_a_gpu_the_model_is_allowed_on_but_not_using_asks_for_a_restart():
    item = advice(facts(), "gpu")
    assert item["level"] == "consider"
    assert item["action"]["kind"] == "restart"


def test_a_gpu_switched_off_offers_to_switch_it_on():
    item = advice(facts(**{"ninaivu.ai_gpu": False}), "gpu")
    assert item["action"] == {"kind": "setting", "key": "ai_gpu", "value": True,
                              "restart": True, "label": "Use it and restart"}


def test_a_gpu_in_use_is_reported_as_fine():
    item = advice(facts(**{"graphics.in_use": "mps", "graphics.ai_device": "mps"}), "gpu")
    assert item["level"] == "good"


def test_a_computer_with_no_gpu_is_not_told_about_one():
    assert advice(facts(**{"graphics.available": None}), "gpu") is None


# --- drives -------------------------------------------------------------------------

def test_a_windows_drive_on_a_mac_is_explained_with_what_to_do():
    item = advice(facts(), "read-only")
    assert item["level"] == "act"
    assert "NTFS" in item["detail"] and "exFAT" in item["detail"] and "APFS" in item["detail"]


def test_a_read_only_drive_elsewhere_is_not_blamed_on_macos():
    item = advice(facts(platform="win32"), "read-only")
    assert item["level"] == "act" and "macOS" not in item["detail"]


def test_a_library_that_is_not_connected_is_said_first():
    given = facts(**{"storage.0": {"role": "library", "path": "E:\\MasterArchive",
                                   "present": False}})
    items = capacity.recommend(given)
    assert items[0]["id"] == "missing:E:\\MasterArchive"


def test_an_index_on_a_usb_disk_is_worth_moving():
    given = facts(**{"storage.1.internal": False, "storage.1.bus": "USB",
                     "storage.1.solid_state": False})
    item = advice(given, "index-drive")
    assert item["level"] == "consider" and "a USB spinning drive" in item["detail"]


def test_an_index_on_the_internal_ssd_is_fine():
    assert advice(facts(), "index-drive")["level"] == "good"


def test_an_index_drive_nearly_full_needs_attention():
    assert advice(facts(**{"storage.1.free_bytes": 3 * GB}), "index-space")["level"] == "act"


def test_a_slow_scan_says_how_slow_and_on_what():
    item = advice(facts(), "scan")
    assert "3 min 40 s" in item["detail"] and "210,157" in item["detail"]
    assert "a USB drive formatted NTFS" in item["detail"]


def test_a_quick_scan_is_not_mentioned():
    given = facts(**{"scans./Volumes/Red/MasterArchive.seconds": 12.0})
    assert advice(given, "scan") is None


# --- memory, battery, the mode ----------------------------------------------------

def test_memory_that_is_fine_is_said_to_be():
    assert advice(facts(), "memory")["level"] == "good"


def test_a_computer_short_of_memory_is_told_so():
    assert advice(facts(**{"load.memory_percent": 94.0}), "memory")["level"] == "act"
    assert advice(facts(**{"load.swap_used_bytes": 6 * GB}), "memory")["level"] == "act"


def test_performance_mode_on_battery_is_questioned():
    given = facts(**{"load.battery": {"percent": 40, "plugged": False}})
    assert advice(given, "battery")["action"]["tab"] == "server"


def test_power_saving_with_work_waiting_on_mains_power_is_questioned():
    given = facts(**{"ninaivu.mode": "power-saving", "backlog.analysis": 5000})
    assert advice(given, "mode")["level"] == "consider"


def test_more_workers_than_cores_is_pointed_out():
    assert advice(facts(**{"ninaivu.workers": 32}), "workers") is not None


def test_work_saved_for_the_night_says_how_much_and_when():
    item = advice(facts(**{"backlog.analysis": 1500}), "overnight")
    assert "1,500" in item["detail"] and "23:00–06:00" in item["detail"]


def test_nothing_waiting_says_nothing_about_the_night():
    assert advice(facts(), "overnight") is None


# --- what is missing ------------------------------------------------------------------

def test_no_ffmpeg_needs_attention():
    assert advice(facts(**{"ninaivu.ffmpeg": False}), "ffmpeg")["level"] == "act"


def test_an_image_model_that_did_not_load_needs_attention():
    assert advice(facts(**{"ninaivu.ai_semantic": False}), "ai-model")["level"] == "act"


def test_the_most_pressing_comes_first():
    levels = [a["level"] for a in capacity.recommend(facts())]
    assert levels == sorted(levels, key=capacity.LEVELS.index)
    assert levels[0] == "act" and levels[-1] == "good"


# --- measuring ------------------------------------------------------------------------

def test_a_folder_is_measured(tmp_path):
    drive = capacity.storage(str(tmp_path), "index")
    assert drive["present"] and drive["read_only"] is False
    assert drive["free_bytes"] > 0 and drive["filesystem"]


def test_a_folder_that_is_not_there_is_said_to_be_missing(tmp_path):
    assert capacity.storage(str(tmp_path / "gone"), "library") == {
        "role": "library", "path": str(tmp_path / "gone"), "present": False}


def test_the_scan_speed_is_the_last_scan_that_found_nothing_new():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, root TEXT, started_at REAL,
            ended_at REAL, added INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
            status TEXT);
        CREATE TABLE assets (root TEXT, trashed INTEGER DEFAULT 0);
        INSERT INTO scan_runs(root, started_at, ended_at, added, status)
            VALUES ('/lib', 0, 900, 4000, 'done'),   -- a real first scan
                   ('/lib', 1000, 1200, 0, 'done'),  -- nothing new: this one
                   ('/lib', 2000, NULL, 0, 'running');
    """)
    conn.executemany("INSERT INTO assets(root) VALUES ('/lib')", [()] * 400)
    assert capacity.scan_speed(conn, "/lib") == {
        "seconds": 200.0, "files": 400, "files_per_second": 2, "at": 1200.0}
    assert capacity.scan_speed(conn, "/other") is None


# --- the page -------------------------------------------------------------------------

def test_the_console_reports_on_this_computer(as_admin):
    data = as_admin.get("/api/admin/performance").get_json()
    assert data["machine"]["logical_cores"] >= 1
    assert {d["role"] for d in data["storage"]} == {"library", "index"}
    assert isinstance(data["recommendations"], list)
    for item in data["recommendations"]:
        assert item["level"] in capacity.LEVELS


def test_only_an_admin_may_see_it(anon, as_family):
    assert anon.get("/api/admin/performance").status_code in (401, 403)
    assert as_family.get("/api/admin/performance").status_code == 403


def test_the_graphics_switch_is_a_setting(as_admin, app):
    response = as_admin.post("/api/admin/settings", json={"ai_gpu": False})
    assert response.status_code == 200
    assert app.config["MV_CONFIG"].ai_gpu is False


@pytest.mark.parametrize("name", ["performance.js", "admin.html"])
def test_the_page_is_in_the_console(name):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ninaivu"
    text = ((root / "static" / "js" / name) if name.endswith(".js")
            else (root / "templates" / name)).read_text(encoding="utf-8")
    assert "performance" in text
    if name == "admin.html":
        assert 'data-tab="performance"' in text and 'data-panel="performance"' in text
