"""A model that is on disk has to be a model that works.

Two things decide whether a downloaded model can be used, and they could
disagree. `installed()` asks whether the files are on disk; the code that runs
the model asks `settings.json` where they are. With the files there and the
setting not, the AI models page said "installed" and so refused to download
again, and the Playground said the feature was unavailable — with nothing on
either page that got out of it.

`settings.json` stores absolute paths, which is the ordinary way into that
state: Ninaivu moved to another folder or another machine keeps pointing at
where its models used to be.
"""

import json

import pytest

from ninaivu.media import model_catalog as mc


@pytest.fixture()
def models(tmp_path, monkeypatch):
    """A models folder with background removal on disk and nothing configured."""
    root = tmp_path / ".ai-models"
    monkeypatch.setattr(mc, "models_root", lambda: root)
    entry = mc.MODELS["segmentation"]["files"][0]
    target = mc.file_path(entry)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\0" * entry["bytes"])
    return root


def settings(root):
    path = root / "settings.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def test_a_model_on_disk_with_no_setting_is_pointed_at(models):
    assert "segmentation_model" not in settings(models)
    assert mc.ensure_settings() == ["segmentation"]
    assert settings(models)["segmentation_model"] == str(
        models / "rmbg-1.4" / "onnx" / "model.onnx")


def test_a_setting_left_over_from_another_machine_is_repaired(models):
    """The migration case: an absolute path to where Ninaivu used to be."""
    (models / "settings.json").write_text(json.dumps(
        {"segmentation_model": r"C:\Old\Ninaivu\.ai-models\rmbg-1.4\onnx\model.onnx"}))
    assert mc.ensure_settings() == ["segmentation"]
    assert settings(models)["segmentation_model"].startswith(str(models))


def test_a_setting_that_names_a_real_file_is_left_alone(models, tmp_path):
    """Somebody pointing at their own copy of a model meant to."""
    elsewhere = tmp_path / "mine.onnx"
    elsewhere.write_bytes(b"x")
    (models / "settings.json").write_text(
        json.dumps({"segmentation_model": str(elsewhere)}))
    assert mc.ensure_settings() == []
    assert settings(models)["segmentation_model"] == str(elsewhere)


def test_the_other_settings_in_the_file_survive(models):
    """The file is shared with setup_ai_models.py; the language model and the
    disk budget are not this function's to throw away."""
    (models / "settings.json").write_text(json.dumps(
        {"language_model": "qwen3:4b", "budget_bytes": 20_000_000_000}))
    mc.ensure_settings()
    stored = settings(models)
    assert stored["language_model"] == "qwen3:4b"
    assert stored["budget_bytes"] == 20_000_000_000


def test_a_model_not_on_disk_is_not_pointed_at(models):
    """Pointing the runtime at a file that is not there would turn "not
    installed" into a stranger failure further along."""
    mc.file_path(mc.MODELS["segmentation"]["files"][0]).unlink()
    assert mc.ensure_settings() == []
    assert "segmentation_model" not in settings(models)


def test_running_it_again_changes_nothing(models):
    mc.ensure_settings()
    before = (models / "settings.json").read_text()
    assert mc.ensure_settings() == []
    assert (models / "settings.json").read_text() == before


def test_a_damaged_settings_file_is_rebuilt_not_fatal(models):
    (models / "settings.json").write_text("{ not json")
    assert mc.ensure_settings() == ["segmentation"]
    assert "segmentation_model" in settings(models)


# --- and the messages somebody sees when it is not there -----------------------

def test_a_missing_model_names_the_console_page_not_a_script(monkeypatch):
    from ninaivu.media import segmentation
    monkeypatch.setattr(segmentation, "model_path", lambda: None)
    with pytest.raises(RuntimeError) as raised:
        segmentation._load()
    assert "AI models" in str(raised.value)
    assert "setup_ai_models" not in str(raised.value)


def test_a_failed_model_download_is_logged(tmp_path, monkeypatch, caplog):
    import logging
    import threading

    root = tmp_path / "models"
    monkeypatch.setattr(mc, "models_root", lambda: root)

    downloader = mc.Downloads()
    finished = threading.Event()

    def failing_opener(*_args, **_kwargs):
        raise OSError("HTTP 404 Not Found")

    with caplog.at_level(logging.WARNING):
        started = downloader.start("segmentation", opener=failing_opener,
                                   on_end=lambda _: finished.set())
        assert started is True
        assert finished.wait(5.0)

    state = downloader.state("segmentation")
    assert state["status"] == "failed"
    assert "404" in state["error"]
    assert any("could not download segmentation" in r.message for r in caplog.records)

