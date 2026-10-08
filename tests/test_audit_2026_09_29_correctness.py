"""Regressions for the correctness findings of the 29 September 2026 audit."""
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.media import orientnet, straighten
from ninaivu.server import capacity, tiers
from ninaivu.server.config import Config


# --- The installers' own folder is not the library ------------------------------

def test_the_installers_say_where_they_live_in_a_name_the_server_does_not_read():
    root = Path(__file__).resolve().parents[1]
    for path in ("installers/windows/ninaivu.nsi", "installers/macos/build.sh"):
        text = (root / path).read_text(encoding="utf-8")
        assert "NINAIVU_HOME" in text
        assert "NINAIVU_ROOT" not in text, f"{path}: the server reads NINAIVU_ROOT as the library"


def test_the_tray_finds_its_home_from_NINAIVU_HOME(tmp_path, monkeypatch):
    from ninaivu.desktop import control
    monkeypatch.setenv("NINAIVU_HOME", str(tmp_path))
    monkeypatch.delenv("NINAIVU_ROOT", raising=False)
    assert control.ninaivu_root() == tmp_path


def test_start_at_sign_in_on_a_mac_carries_the_apps_own_python(tmp_path, monkeypatch):
    from ninaivu.desktop import autostart
    monkeypatch.setenv("NINAIVU_HOME", str(tmp_path))
    monkeypatch.setenv("NINAIVU_PYTHON", "/Applications/Ninaivu.app/Contents/Resources/python/bin/python3")
    agent = autostart.launch_agent(tmp_path)
    assert agent["EnvironmentVariables"] == {
        "NINAIVU_HOME": str(tmp_path),
        "NINAIVU_PYTHON": "/Applications/Ninaivu.app/Contents/Resources/python/bin/python3"}
    assert agent["ProgramArguments"][0].endswith("python3")


# --- Hardware tiers -------------------------------------------------------------------

def test_an_nvidia_pc_is_full_before_anything_has_imported_torch(monkeypatch):
    """Asking torch only when it was already loaded classed every NVIDIA
    machine as Basic at start — so "auto" meant the light engine on all of them."""
    import importlib.util
    import shutil
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delenv("NINAIVU_HARDWARE_TIER", raising=False)
    capacity._nvidia_name.cache_clear()
    real_find = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: object() if name == "torch" else real_find(name, *a))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(capacity, "_run", lambda *cmd, timeout=5.0: "NVIDIA GeForce RTX 4070")
    monkeypatch.setattr(capacity, "machine", lambda: {
        "system": "Windows", "processor": "x", "logical_cores": 8, "physical_cores": 8,
        "core_kinds": [], "memory_bytes": 32 * 1024 ** 3, "apple_silicon": False})
    try:
        tier = tiers.current(Config())
    finally:
        capacity._nvidia_name.cache_clear()
    assert tier["tier"] == "full", tier
    assert "RTX 4070" in tier["why"]


def test_without_torch_there_is_nothing_to_use_the_card_with(monkeypatch):
    import importlib.util
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    capacity._nvidia_name.cache_clear()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    try:
        assert capacity._nvidia_name() is None
    finally:
        capacity._nvidia_name.cache_clear()


def _loaded(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(state))
    cfg = Config.load()
    assert cfg.state_dir == state
    return cfg


def test_the_tiers_numbers_are_not_saved_as_the_households_choice(tmp_path, monkeypatch):
    monkeypatch.delenv("NINAIVU_HARDWARE_TIER", raising=False)
    cfg = _loaded(tmp_path, monkeypatch)
    tiers.apply(cfg, "basic")
    assert cfg.video_keyframes == 1 and cfg.clip_batch_size == 4
    cfg.house_name = "The Kumar Home"
    cfg.save()
    saved = json.loads(cfg.config_path.read_text())
    assert "video_keyframes" not in saved and "clip_batch_size" not in saved

    # The machine gets a GPU: Full's numbers, not Basic's frozen in the file.
    again = _loaded(tmp_path, monkeypatch)
    tiers.apply(again, "full")
    assert again.video_keyframes == 5 and again.clip_batch_size == 16


def test_a_household_choice_back_to_the_shipped_default_is_kept_on_a_basic_machine(tmp_path, monkeypatch):
    monkeypatch.delenv("NINAIVU_HARDWARE_TIER", raising=False)
    cfg = _loaded(tmp_path, monkeypatch)
    tiers.apply(cfg, "basic")
    cfg.video_keyframes = 5                      # "describe videos by five moments", chosen here
    cfg.save()
    again = _loaded(tmp_path, monkeypatch)
    tiers.apply(again, "basic")
    assert again.video_keyframes == 5, "the household's choice survives the next start"


def test_a_tier_set_in_the_environment_is_not_written_down(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_HARDWARE_TIER", "basic")
    cfg = _loaded(tmp_path, monkeypatch)
    assert tiers.chosen(cfg) == "basic"
    cfg.house_name = "x"
    cfg.save()
    assert "hardware_tier" not in json.loads(cfg.config_path.read_text())


# --- Straightening: what it has looked at, and when -------------------------------

@pytest.fixture()
def survey(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.straighten_requires_face = False
    straighten.init_schema(conn)
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)
    looked = []

    def predict(img, state_dir=None):
        looked.append(1)
        return (0, 0.99)
    monkeypatch.setattr(orientnet, "predict", predict)
    return straighten.Straightener(cfg), cfg, conn, looked


def _run(job):
    thread = job._thread
    if thread:
        thread.join(30)


def _left(job):
    """What the survey had still to look at: the total counts the whole
    library, with what earlier runs looked at already done."""
    state = job.progress.snapshot()
    return state["total"] - state["earlier"]


def test_a_photograph_that_could_not_be_read_is_looked_at_again(survey, monkeypatch):
    job, cfg, conn, looked = survey
    from ninaivu.media import media
    real = media.open_for_index
    first = conn.execute("SELECT MIN(id) FROM assets WHERE kind='picture'").fetchone()[0]
    broken = conn.execute("SELECT root, rel_path FROM assets WHERE id=?", (first,)).fetchone()
    target = str(Path(broken["root"]) / broken["rel_path"])

    def flaky(path, edge):
        if str(path) == target:
            raise OSError("the drive is busy")
        return real(path, edge)
    monkeypatch.setattr(media, "open_for_index", flaky)
    job.survey([cfg.active_root])
    _run(job)
    monkeypatch.setattr(media, "open_for_index", real)
    job.survey([cfg.active_root])
    _run(job)
    assert _left(job) == 1, "only the one that failed, and it is tried again"


def test_a_file_that_changed_is_looked_at_again(survey):
    job, cfg, conn, looked = survey
    job.survey([cfg.active_root])
    _run(job)
    one = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()[0]
    conn.execute("UPDATE assets SET indexed_at = indexed_at + 100 WHERE id=?", (one,))
    conn.commit()
    job.survey([cfg.active_root])
    _run(job)
    assert _left(job) == 1


def test_a_drive_that_was_away_is_not_skipped_when_it_comes_back(survey, tmp_path):
    """One watermark for every drive skipped the older photographs of a drive
    that was unplugged while another was surveyed."""
    job, cfg, conn, looked = survey
    other = tmp_path / "away"
    other.mkdir()
    Image.new("RGB", (320, 240), "blue").save(other / "old.jpg")
    # The away drive's photograph was indexed first (the lower id).
    conn.execute("UPDATE assets SET id = id + 1000")
    conn.execute(
        "INSERT INTO assets(id, root, rel_path, folder, filename, kind, size, mtime, indexed_at, "
        "rotation, rot_source, trashed) VALUES (1, ?, 'old.jpg', '', 'old.jpg', 'picture', 1, 0, 1, 0, 'none', 0)",
        (str(other),))
    conn.commit()
    job.survey([cfg.active_root])                 # the away drive is not in the list
    _run(job)
    job.survey([cfg.active_root, str(other)])     # it is back
    _run(job)
    assert _left(job) == 1


def test_the_survey_after_a_scan_waits_for_the_scan_to_finish(scanned, monkeypatch):
    """Started from inside the scan's "done", it held the scanner aside — which
    stopped the rest of the scan and queued another walk of every library."""
    from ninaivu import build_services
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.straighten_requires_face = False
    cfg.straighten_auto = True
    monkeypatch.setattr(orientnet, "available", lambda state_dir=None: True)
    monkeypatch.setattr(orientnet, "predict", lambda img, state_dir=None: (0, 0.99))
    services = build_services(cfg)
    services.scanner.stop()
    walks = []
    real = services.scanner._run_all
    monkeypatch.setattr(services.scanner, "_run_all",
                        lambda roots, full: (walks.append(1), real(roots, full))[1])
    Image.new("RGB", (300, 200), (1, 2, 3)).save(Path(cfg.active_root) / "new.jpg")
    services.scanner.start()
    deadline = time.time() + 20
    surveyed = False
    while time.time() < deadline:
        time.sleep(0.1)
        surveyed = surveyed or services.straightener.progress.snapshot()["status"] == "done"
        if walks and surveyed and not services.scanner.running and not services.straightener.running:
            break
    services.stop(timeout=5)
    assert surveyed, "the survey ran"
    assert len(walks) == 1, f"the library was walked {len(walks)} times for one scan"


# --- Takeout albums -------------------------------------------------------------------

@pytest.mark.parametrize("title,album", [
    ("Photos from 2019", False), ("Fotos von 2019", False), ("Untitled(2)", False),
    ("Trash", False), ("Bin", False), ("Bindu's wedding", True), ("Archived letters", True),
    ("Trashing the garden", True), ("Untitled road trip", True), ("Paris 2019", True),
])
def test_takeouts_own_folders_are_matched_whole(tmp_path, title, album):
    from ninaivu.archive import takeout
    folder = tmp_path / "a"
    folder.mkdir()
    (folder / "metadata.json").write_text(json.dumps({"title": title}), encoding="utf-8")
    assert takeout.is_album_folder(folder) is album



def test_offline_mode_reaches_the_library_that_already_read_its_environment(monkeypatch):
    """huggingface_hub reads HF_HUB_OFFLINE once, at import; setting only the
    environment afterwards changed nothing."""
    import types
    from ninaivu import ai
    constants = types.SimpleNamespace(HF_HUB_OFFLINE=False, HF_HUB_DISABLE_TELEMETRY=False)
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", constants)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    ai.go_offline()
    assert constants.HF_HUB_OFFLINE is True and constants.HF_HUB_DISABLE_TELEMETRY is True
