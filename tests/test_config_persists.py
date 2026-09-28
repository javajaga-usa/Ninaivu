"""Every setting a household changes has to survive the next thing they change.

`Config.save()` used to write a hand-written list of keys, and a setting left
off it was dropped the next time anything at all was saved: set it by hand,
flip one switch in the console, and it was gone with no message anywhere. Forty
two of the ninety-seven settings were in that position. One of them was
`video_keyframes`, where the cost of the silence was another fifteen hours of
describing videos on a large library.

The list is inverted now — everything but the settings that belong to how the
process was started — and these hold that, including the property that makes it
safe: a setting equal to its default is not written at all, so a household that
never touched a threshold still gets a better one in a later version.
"""

import json

import pytest

from ninaivu.server.config import (
    RUNTIME_ONLY, Config, _field_defaults)


@pytest.fixture()
def saved(tmp_path):
    """A config writing to a directory of its own, and a way to read it back."""
    cfg = Config()
    cfg.state_dir = tmp_path / "state"

    def write() -> dict:
        cfg.save()
        return json.loads((cfg.state_dir / "config.json").read_text("utf-8"))

    return cfg, write


# --- nothing is silently dropped -------------------------------------------

def test_no_setting_is_left_out_of_the_file(saved):
    """The guard for the whole class of bug. Every field is either written or
    named as belonging to the process rather than the household."""
    cfg, write = saved
    for name in _field_defaults():
        if name in RUNTIME_ONLY:
            continue
        setattr(cfg, name, _changed(getattr(cfg, name)))
    stored = write()
    missing = [n for n in _field_defaults()
               if n not in RUNTIME_ONLY and n not in stored]
    assert not missing, f"changed but not written back: {missing}"


def _changed(value):
    """Some value of the same shape that is not the one passed in."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return f"{value}x"
    if isinstance(value, tuple):
        return value + (999,)
    if isinstance(value, (set, frozenset)):
        return set(value) | {"zzz"}
    if isinstance(value, list):
        return [*value, "zzz"]
    return "set-by-hand"


def test_the_setting_that_started_this_survives(saved):
    cfg, write = saved
    cfg.video_keyframes = 0
    assert write()["video_keyframes"] == 0
    # ...and is still there after something entirely unrelated is saved.
    cfg.house_name = "The Kumar Home"
    stored = write()
    assert stored["video_keyframes"] == 0
    assert stored["house_name"] == "The Kumar Home"


@pytest.mark.parametrize("name,value", [
    ("ocr_enabled", True),
    ("perceptual_hash", False),
    ("detect_orientation", False),
    ("rescan_on_change", False),
    ("thumb_quality", 95),
    ("nsfw_threshold", 0.8),
    ("max_tags", 3),
    ("boot_scan_after", 0.0),
])
def test_settings_that_used_to_be_dropped(saved, name, value):
    """A sample of the forty-two. Each was lost on the next save."""
    cfg, write = saved
    setattr(cfg, name, value)
    assert write()[name] == value


# --- but a default is not frozen into the file -----------------------------

def test_an_untouched_install_writes_nothing(saved):
    """What keeps the inversion safe. Writing every setting down would freeze
    today's defaults into every household's file, and a better threshold in a
    later version would never reach the people who never chose one."""
    _, write = saved
    assert write() == {}


def test_a_setting_put_back_to_its_default_leaves_the_file(saved):
    cfg, write = saved
    cfg.video_keyframes = 0
    assert "video_keyframes" in write()
    cfg.video_keyframes = Config.video_keyframes
    assert "video_keyframes" not in write()


def test_a_tuple_read_back_as_a_list_does_not_count_as_a_change(saved):
    """`thumb_sizes` is a tuple in the dataclass and a list in JSON. Compared
    naively, every save would write it and every load would look modified."""
    cfg, write = saved
    cfg.thumb_sizes = list(Config.thumb_sizes)
    assert "thumb_sizes" not in write()
    cfg.thumb_sizes = (256, 640, 1280)
    assert write()["thumb_sizes"] == [256, 640, 1280]


def test_a_set_in_a_different_order_does_not_count_as_a_change(saved):
    cfg, write = saved
    cfg.ignore_dirs = set(reversed(sorted(cfg.ignore_dirs)))
    assert "ignore_dirs" not in write()


# --- and the process's own settings stay out of it -------------------------

def test_how_the_process_was_started_is_never_written_down(saved):
    """A one-off `--port 8080` must not become permanent, and the state
    directory must not be written into a file inside itself."""
    cfg, write = saved
    cfg.port = 8080
    cfg.host = "10.0.0.5"
    cfg.debug = True
    stored = write()
    assert not (RUNTIME_ONLY & set(stored)), sorted(RUNTIME_ONLY & set(stored))


def test_what_is_written_is_what_comes_back(saved):
    """The round trip, which is the only thing any of this is for."""
    cfg, write = saved
    cfg.video_keyframes = 2
    cfg.ocr_enabled = True
    cfg.house_name = "Casa Chaos"
    stored = write()

    fresh = Config()
    for key, value in stored.items():
        if hasattr(fresh, key):
            setattr(fresh, key, value)
    assert fresh.video_keyframes == 2
    assert fresh.ocr_enabled is True
    assert fresh.house_name == "Casa Chaos"


def test_the_file_is_json_a_person_can_read(saved):
    cfg, write = saved
    cfg.roots = ["E:/MasterArchive"]
    text = (cfg.state_dir / "config.json")
    write()
    assert "\n" in text.read_text("utf-8"), "indented, not one long line"
