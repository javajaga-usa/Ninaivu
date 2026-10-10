"""Tuning: Ninaivu's numbers sized to the machine it runs on, and the
console page where an administrator sees them and sets them."""

import math

import pytest

from ninaivu.server import tuning
from ninaivu.server.config import Config

GB = 1024 ** 3

PI = {"cores": 4, "memory_bytes": 8 * GB, "board": "Raspberry Pi 4 Model B Rev 1.4",
      "gpu": None, "apple_silicon": False, "library_spinning": True, "tier": "basic",
      "ai_enabled": True, "mode": None}
LAPTOP = {"cores": 4, "memory_bytes": 8 * GB, "board": None, "gpu": None,
          "apple_silicon": False, "library_spinning": None, "tier": "basic",
          "ai_enabled": True, "mode": None}
MAC_MINI = {"cores": 10, "memory_bytes": 16 * GB, "board": None, "gpu": "mps",
            "apple_silicon": True, "library_spinning": False, "tier": "full",
            "ai_enabled": True, "mode": None}
WORKSTATION = {"cores": 24, "memory_bytes": 64 * GB, "board": None, "gpu": "cuda",
               "apple_silicon": False, "library_spinning": False, "tier": "full",
               "ai_enabled": True, "mode": None}


@pytest.fixture(autouse=True)
def _no_mode_from_the_environment(monkeypatch):
    for name in ("NINAIVU_RESOURCE_MODE", "NINAIVU_SERVER_THREADS", "NINAIVU_COMPUTE_THREADS",
                 "NINAIVU_WORKERS", "NINAIVU_HARDWARE_TIER"):
        monkeypatch.delenv(name, raising=False)


# -- the profile follows the machine -----------------------------------------

@pytest.mark.parametrize("machine,expected", [
    (PI, "small"), (LAPTOP, "medium"), (MAC_MINI, "large"), (WORKSTATION, "large"),
    ({**LAPTOP, "memory_bytes": 4 * GB}, "small"),
    ({**LAPTOP, "cores": 2}, "small"),
    ({**LAPTOP, "cores": 6, "memory_bytes": 16 * GB, "gpu": "cuda"}, "large"),
    ({**LAPTOP, "memory_bytes": None}, "medium"),
])
def test_the_profile_follows_the_machine(machine, expected):
    assert tuning.detect(machine)[0] == expected


def test_a_raspberry_pi_says_so():
    profile, why, params = tuning.detect(PI)
    assert profile == "small" and params["board"].startswith("Raspberry Pi")
    assert "{board}" in why


def test_peak_is_only_ever_asked_for():
    for machine in (PI, LAPTOP, MAC_MINI, WORKSTATION):
        assert tuning.detect(machine)[0] != "peak"


def test_the_desktop_panels_mode_picks_the_profile():
    cfg = Config()
    assert tuning.chosen_profile(cfg, {**MAC_MINI, "mode": "performance"}) == ("peak", "mode")
    assert tuning.chosen_profile(cfg, {**MAC_MINI, "mode": "power-saving"}) == ("small", "mode")
    cfg.tuning_profile = "medium"
    assert tuning.chosen_profile(cfg, {**MAC_MINI, "mode": "performance"}) == ("medium", "set")


# -- what each profile is given ------------------------------------------------

def test_a_pi_leaves_most_of_itself_to_the_system():
    plan = tuning.plan(PI, "small")
    values = plan["values"]
    assert values["workers"] <= 2 and values["compute_threads"] <= 2
    assert values["clip_batch_size"] <= 4 and values["video_keyframes"] == 1
    assert plan["usage"]["cpu_percent"] <= 50 and not plan["usage"]["over"]


def test_a_mac_mini_keeps_two_cores_back():
    values = tuning.plan(MAC_MINI, "large")["values"]
    assert values["workers"] == 8 and values["compute_threads"] == 8
    assert values["video_keyframes"] == 5


@pytest.mark.parametrize("machine", [PI, LAPTOP, MAC_MINI, WORKSTATION])
def test_peak_uses_up_to_95_percent_and_never_more(machine):
    plan = tuning.plan(machine, "peak")
    usage = plan["usage"]
    assert usage["ceiling_percent"] == 95
    assert usage["cpu_percent"] <= 95
    assert usage["memory_percent"] <= 95
    assert not usage["over"]
    # and it is more than the measured profile gives, not the same
    measured = tuning.plan(machine, tuning.detect(machine)[0])
    assert plan["values"]["compute_threads"] >= measured["values"]["compute_threads"]


def test_peak_on_twenty_cores_is_nineteen():
    # No room needed for the survey beside the scan: the library's disk is unknown.
    values = tuning.plan({**WORKSTATION, "cores": 20, "library_spinning": None}, "peak")["values"]
    assert values["workers"] == 19 and values["compute_threads"] == 19


def test_peak_on_twenty_cores_shares_nineteen_with_the_survey_beside_the_scan():
    """With a graphics processor and a solid-state library the survey runs
    beside the scan; its readers come out of the same 95 %."""
    plan = tuning.plan({**WORKSTATION, "cores": 20}, "peak")
    assert plan["values"]["compute_threads"] == 19
    assert plan["values"]["workers"] + plan["survey_beside"] == 19
    assert plan["survey_beside"] == tuning.SURVEY_BESIDE
    assert plan["usage"]["cores"] == 19 and not plan["usage"]["over"]


@pytest.mark.parametrize("cores, workers, survey", [
    (18, 15, 2),    # the 18-core MacBook Pro: 17 cores busy, not 12
    (14, 11, 2),
    (12, 9, 2),
    (10, 7, 2),     # the Mac mini
    (4, 3, 0),      # too small to run the survey beside the scan: it holds it
])
def test_performance_on_a_mac_uses_95_percent_of_every_core(cores, workers, survey):
    plan = tuning.plan({**MAC_MINI, "cores": cores, "memory_bytes": 32 * GB}, "peak")
    assert plan["values"]["workers"] == workers
    assert plan["values"]["compute_threads"] == math.floor(cores * 0.95)
    assert plan["survey_beside"] == survey
    assert plan["usage"]["cores"] == math.floor(cores * 0.95)


@pytest.mark.parametrize("machine", [PI, LAPTOP, MAC_MINI, WORKSTATION,
                                     {**MAC_MINI, "cores": 18}, {**MAC_MINI, "cores": 6}])
@pytest.mark.parametrize("profile", tuning.PROFILES)
def test_the_scan_and_the_survey_beside_it_stay_within_the_ceiling(machine, profile):
    plan = tuning.plan(machine, profile)
    share = math.floor(machine["cores"] * tuning.CEILING[profile])
    assert plan["survey_beside"] == 0 or plan["survey_beside"] >= 2
    if plan["survey_beside"]:
        assert plan["values"]["workers"] + plan["survey_beside"] <= share
    assert plan["usage"]["cores"] == min(machine["cores"], max(
        plan["values"]["workers"] + plan["survey_beside"], plan["values"]["compute_threads"]))


@pytest.mark.parametrize("cores, workers", [(4, 2), (5, 3), (6, 4), (8, 6)])
def test_balanced_on_a_small_large_machine_stays_within_80_percent(cores, workers):
    """Four cores with a graphics processor and 16 GB measure as large; the
    old floor of four workers planned all four cores, past the 80 %."""
    plan = tuning.plan({**MAC_MINI, "cores": cores}, "large")
    assert plan["values"]["workers"] == workers
    assert plan["values"]["compute_threads"] == workers
    assert not plan["usage"]["over"]


def test_balanced_on_eighteen_cores_is_not_held_at_twelve():
    """Large had a fixed maximum of twelve: an 18-core Mac in Balanced left a
    third of itself idle. It is 80 % now, two cores kept back at least."""
    values = tuning.plan({**MAC_MINI, "cores": 18, "memory_bytes": 36 * GB}, "large")["values"]
    assert values["workers"] == 14 and values["compute_threads"] == 14
    assert values["server_threads"] == 18
    values = tuning.plan({**WORKSTATION, "cores": 32}, "large")["values"]
    assert values["workers"] == 25


def test_a_spinning_disk_is_not_read_by_more_than_four():
    plan = tuning.plan({**WORKSTATION, "library_spinning": True}, "peak")
    knob = next(k for k in plan["knobs"] if k["name"] == "workers")
    assert knob["value"] == tuning.SPINNING_READERS
    assert "spinning disk" in knob["note"]


def test_a_small_memory_shrinks_the_batch_first():
    tight = {**WORKSTATION, "memory_bytes": 4 * GB}
    plan = tuning.plan(tight, "peak")
    assert plan["usage"]["memory_percent"] <= 95
    batch = next(k for k in plan["knobs"] if k["name"] == "clip_batch_size")
    assert batch["value"] < 64 and batch["note"]


# -- who decides each number ----------------------------------------------------

def test_an_administrators_number_wins_and_says_so():
    plan = tuning.plan(MAC_MINI, "large", overrides={"workers": 3}, startup={"workers": 5})
    knob = next(k for k in plan["knobs"] if k["name"] == "workers")
    assert (knob["value"], knob["source"], knob["auto"]) == (3, "set", 8)


def test_a_start_up_number_stands_over_the_automatic_one():
    plan = tuning.plan(MAC_MINI, "large", startup={"server_threads": 20})
    knob = next(k for k in plan["knobs"] if k["name"] == "server_threads")
    assert (knob["value"], knob["source"]) == (20, "startup")


def test_numbers_past_the_ceiling_are_flagged_not_refused():
    plan = tuning.plan(PI, "small", overrides={"workers": 4, "compute_threads": 4})
    assert plan["usage"]["cpu_percent"] == 100 and plan["usage"]["over"]


def test_a_hand_edited_file_cannot_start_five_hundred_threads():
    assert tuning.clean_overrides({"workers": 500, "nonsense": 3, "db_cache_mb": "x",
                                   "server_threads": True}) == {"workers": 64}
    assert tuning.clean_overrides(["workers"]) == {}


def test_saving_checks_each_value():
    cfg = Config()
    tuning.save(cfg, "peak", {"workers": 6})
    assert cfg.tuning_profile == "peak" and cfg.tuning == {"workers": 6}
    tuning.save(cfg, None, {"workers": None})
    assert cfg.tuning == {}
    for bad in ({"workers": 0}, {"workers": "six"}, {"nothing": 1}, {"workers": True}):
        with pytest.raises(ValueError):
            tuning.save(cfg, None, bad)
    with pytest.raises(ValueError):
        tuning.save(cfg, "turbo")
    tuning.save(cfg, reset=True)
    assert cfg.tuning_profile == "auto" and cfg.tuning == {}


# -- the running server ---------------------------------------------------------

def test_apply_sets_the_numbers_without_making_them_the_households(tmp_path):
    cfg = Config(state_dir=tmp_path)
    cfg._chosen = set()
    tuning.apply(cfg, machine=dict(PI))
    assert cfg.workers == 2 and cfg.clip_batch_size == 4
    cfg.save()
    saved = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert '"workers"' not in saved and '"clip_batch_size"' not in saved


def test_a_number_in_config_json_is_kept(tmp_path):
    cfg = Config(state_dir=tmp_path)
    cfg.clip_batch_size = 2
    cfg._chosen = {"clip_batch_size"}
    result = tuning.apply(cfg, machine=dict(MAC_MINI))
    assert cfg.clip_batch_size == 2
    assert next(k for k in result["knobs"] if k["name"] == "clip_batch_size")["source"] == "startup"


def test_an_override_reaches_the_parts_that_read_it(tmp_path):
    from ninaivu.storage import db
    from ninaivu.utils import resources

    cfg = Config(state_dir=tmp_path)
    cfg._chosen = set()
    cfg.tuning = {"compute_threads": 3, "db_cache_mb": 24, "workers": 5}
    try:
        tuning.apply(cfg, machine=dict(MAC_MINI))
        assert resources.compute_threads() == 3
        assert db.CACHE_MB == 24
        assert cfg.workers == 5
    finally:
        resources._TUNED.clear()
        db.set_cache_mb(16)


def test_a_restart_is_asked_for_only_by_what_needs_one(tmp_path):
    cfg = Config(state_dir=tmp_path)
    cfg._chosen = set()
    first = tuning.apply(cfg, machine=dict(MAC_MINI))
    assert first["restart_needed"] == []
    cfg.tuning = {"workers": 3}
    assert tuning.apply(cfg, machine=dict(MAC_MINI))["restart_needed"] == []
    cfg.tuning = {"server_threads": 30}
    assert tuning.apply(cfg, machine=dict(MAC_MINI))["restart_needed"] == ["server_threads"]


# -- the console --------------------------------------------------------------------

def test_the_console_shows_the_tuning(as_admin):
    data = as_admin.get("/api/admin/tuning").get_json()
    assert data["profile"] in tuning.PROFILES
    assert {k["name"] for k in data["knobs"]} == set(tuning.KNOBS)
    assert data["usage"]["of_cores"] >= 1
    assert [p["id"] for p in data["profiles"]] == list(tuning.PROFILES)


def test_only_an_admin_may_tune(anon, as_family):
    assert anon.get("/api/admin/tuning").status_code in (401, 403)
    assert as_family.get("/api/admin/tuning").status_code in (401, 403)
    assert as_family.post("/api/admin/tuning", json={"reset": True}).status_code in (401, 403)


def test_the_console_saves_a_profile_and_a_number_and_resets(as_admin, app):
    cfg = app.config["MV_CONFIG"]
    reply = as_admin.post("/api/admin/tuning", json={"profile": "peak", "values": {"workers": 2}})
    assert reply.status_code == 200, reply.get_json()
    data = reply.get_json()
    assert data["profile"] == "peak" and data["profile_source"] == "set"
    assert data["values"]["workers"] == 2 and cfg.workers == 2
    assert '"tuning_profile": "peak"' in (cfg.state_dir / "config.json").read_text(encoding="utf-8")
    reply = as_admin.post("/api/admin/tuning", json={"reset": True})
    assert reply.get_json()["setting"] == "auto" and cfg.tuning == {}


@pytest.mark.parametrize("body", [{"profile": "turbo"}, {"values": {"workers": 0}},
                                  {"values": [1]}, {"what": 1}])
def test_the_console_refuses_what_it_cannot_use(as_admin, body):
    assert as_admin.post("/api/admin/tuning", json=body).status_code == 400


@pytest.mark.parametrize("name", ["admin.html"])
def test_the_page_is_in_the_console(name):
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "ninaivu"
    text = (root / "templates" / name).read_text(encoding="utf-8")
    assert 'data-tab="tuning"' in text and 'data-panel="tuning"' in text
    script = (root / "static" / "js" / "admin.js").read_text(encoding="utf-8")
    assert "TuningPanel" in script and (root / "static" / "js" / "tuning.js").is_file()
