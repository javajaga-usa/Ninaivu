"""Maintenance mode: the whole computer for the administrators' work.

The fourth choice under Activity → Sharing this computer (server/workload.py):
nothing makes way and nothing waits for the computer to be idle, the Tuning
profile is Peak while it lasts, family members are shown a downtime notice
instead of the gallery, and leaving it puts back what was there before.
"""

import time
from types import SimpleNamespace

import pytest

from ninaivu.server import workload as wl


def make(mode="balanced", **extra):
    cfg = SimpleNamespace(workload_mode=mode, workload_night_start="23:00",
                          workload_night_end="06:00", tuning_profile="auto",
                          maintenance_before={}, **extra)
    return cfg, wl.Workload(cfg)


# -- the rule ---------------------------------------------------------------------

def test_nothing_makes_way_in_maintenance():
    cfg, work = make("overnight", daily_catchup=True)
    work.noticed("/api/file/12", ranged=True)          # a video playing
    work.noticed("/api/segments")                      # somebody browsing
    assert work.hold(wl.ANALYSIS)                       # held by day, as before
    wl.set_mode(cfg, wl.MAINTENANCE)
    for job in wl.JOBS:
        assert work.hold(job) is None
        assert work.boost(job)
    assert work.catchup_hold() is None
    assert work.catchup_window().always_open


def test_the_daily_catch_up_starts_in_maintenance_even_when_switched_off():
    cfg, work = make(daily_catchup=False)
    assert not work.catchup_on
    wl.set_mode(cfg, wl.MAINTENANCE, now=1000.0)
    assert work.catchup_on
    assert work.maintenance_since == 1000.0


def test_leaving_puts_the_mode_and_the_tuning_profile_back():
    cfg, _ = make("quiet")
    cfg.tuning_profile = "medium"
    assert wl.set_mode(cfg, wl.MAINTENANCE, now=5.0) is True
    assert cfg.tuning_profile == "peak"
    assert cfg.maintenance_before == {"mode": "quiet", "tuning_profile": "medium", "since": 5.0}
    snap = wl.maintenance_snapshot(cfg)
    assert snap == {"on": True, "since": 5.0, "returns_to": "quiet"}
    # Choosing maintenance again does not forget what came before it.
    assert wl.set_mode(cfg, wl.MAINTENANCE) is False
    assert cfg.maintenance_before["mode"] == "quiet"
    assert wl.leave_maintenance(cfg) is True
    assert (cfg.workload_mode, cfg.tuning_profile, cfg.maintenance_before) == \
        ("quiet", "medium", {})
    assert wl.leave_maintenance(cfg) is False          # nothing to leave


def test_peak_already_needs_no_retune():
    cfg, _ = make()
    cfg.tuning_profile = "peak"
    assert wl.set_mode(cfg, wl.MAINTENANCE) is False
    assert wl.set_mode(cfg, wl.BALANCED) is False
    assert cfg.tuning_profile == "peak"


def test_the_catch_up_starts_the_owed_work_once_per_maintenance():
    from ninaivu.server.catchup import DailyCatchUp

    cfg, work = make(daily_catchup=False, libraries=[], active_root="")
    started = []
    scanner = SimpleNamespace(running=False, _thread=None,
                              start=lambda roots, scopes=None: started.append(roots))
    catchup = DailyCatchUp(cfg, work, scanner, None, owed=lambda: ["/lib"])
    catchup._save = lambda: None
    assert catchup.look() == "off"
    wl.set_mode(cfg, wl.MAINTENANCE, now=time.time())
    assert catchup.look() == "scan"
    assert catchup.look() == "nothing"
    wl.set_mode(cfg, wl.BALANCED)
    wl.set_mode(cfg, wl.MAINTENANCE, now=time.time() + 60)
    assert catchup.look() == "scan"
    assert len(started) == 2


# -- the apps ---------------------------------------------------------------------

@pytest.fixture()
def apps(scanned):
    from ninaivu import build_services, create_admin_app, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    yield {"home": create_home_app(services), "admin": create_admin_app(services),
           "services": services}
    services.stop(timeout=5.0)


def test_family_members_get_the_downtime_notice_and_admins_carry_on(apps, people):
    from conftest import ADMIN, FAMILY, login

    console = login(apps["admin"].test_client(), *ADMIN)
    family = login(apps["home"].test_client(), *FAMILY)
    admin_at_home = login(apps["home"].test_client(), *ADMIN)
    assert family.get("/api/assets").status_code == 200

    started = console.post("/api/admin/workload", json={"mode": "maintenance"})
    assert started.status_code == 200, started.get_json()
    data = started.get_json()
    assert data["mode"] == "maintenance" and data["maintenance"]["on"]
    assert data["maintenance"]["returns_to"] == "balanced"
    assert all(job["holding"] is None and job["boost"] for job in data["jobs"])
    cfg = apps["services"].cfg
    assert cfg.tuning_profile == "peak"

    refused = family.get("/api/assets")
    assert refused.status_code == 503
    assert refused.get_json()["maintenance"] is True
    assert refused.headers["Retry-After"]
    page = family.get("/share/abc")
    assert page.status_code == 503 and b"Down for maintenance" in page.data
    # The app itself, signing in, and the question of whether it is over.
    assert family.get("/").status_code == 200
    assert family.get("/api/me").status_code == 200
    assert family.get("/api/maintenance").get_json()["on"] is True
    # Administrators keep both apps.
    assert admin_at_home.get("/api/assets").status_code == 200
    assert console.get("/api/admin/workload").status_code == 200

    ended = console.post("/api/admin/workload", json={"leave_maintenance": True})
    assert ended.status_code == 200
    assert ended.get_json()["mode"] == "balanced"
    assert cfg.tuning_profile == "auto" and cfg.maintenance_before == {}
    assert family.get("/api/assets").status_code == 200
    assert family.get("/api/maintenance").get_json() == {
        "on": False, "since": None, "returns_to": None}


def test_advanced_settings_leave_the_mode_to_its_page(apps, people):
    from conftest import ADMIN, login

    console = login(apps["admin"].test_client(), *ADMIN)
    refused = console.post("/api/admin/settings/all", json={"workload_mode": "maintenance"})
    assert refused.status_code == 400
    assert "Sharing this computer" in refused.get_json()["error"]
