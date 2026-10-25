"""The fewer-switches list (3.1): the proven trials are on, the features that
need a model follow the AI models page, and tuning leaves the Advanced page."""
from dataclasses import fields

from ninaivu.server import features, settings_groups
from ninaivu.server.config import Config


def test_the_page_shows_thirty_three_switches():
    shown = [s for g in settings_groups.describe(Config())["groups"] for s in g["settings"]
             if s["kind"] == "bool"]
    assert len(shown) == 33
    assert not {s["name"] for s in shown} & settings_groups.OFF_THE_PAGE


def test_every_switch_off_the_page_is_still_a_setting_in_its_group():
    names = {f.name for f in fields(Config)}
    assert settings_groups.OFF_THE_PAGE <= names
    assert all(settings_groups.group_of(n) for n in settings_groups.OFF_THE_PAGE)
    # So the Straighten page can still turn its own on and off.
    cfg = Config()
    assert settings_groups.apply(cfg, {"straighten_auto": False}) == ["straighten_auto"]
    assert cfg.straighten_auto is False


def test_the_proven_trials_and_the_model_features_are_on_by_default():
    cfg = Config()
    for name in (*settings_groups.ALWAYS_ON, *settings_groups.FOLLOW_THEIR_MODEL):
        assert getattr(cfg, name) is True, name
    assert set(settings_groups.FOLLOW_THEIR_MODEL) == set(features.NEEDS)


def test_a_feature_that_needs_a_model_is_on_only_with_it(monkeypatch):
    features.forget()
    there = {"writing": False}
    monkeypatch.setattr(features, "_why_not", lambda need: None if there.get(need) else "not here")
    cfg = Config()
    assert not features.on(cfg, "memory_films")
    there["writing"] = True
    assert not features.on(cfg, "memory_films"), "kept a while"
    features.forget()
    assert features.on(cfg, "memory_films")
    # Turned off by the household, it stays off with the model there.
    cfg.memory_films = False
    assert not features.on(cfg, "memory_films")
    # Moments inside videos need only the gallery's search.
    assert features.on(Config(), "video_moments")
    features.forget()


def test_a_saved_false_still_turns_one_off(tmp_path):
    import json
    cfg = Config(state_dir=tmp_path)
    cfg.photo_frame = False
    cfg.save()
    saved = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    assert saved["photo_frame"] is False
    assert "time_capsules" not in saved             # the default, on, is not written
