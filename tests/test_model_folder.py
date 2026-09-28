"""Every AI model in one folder, so that a machine move is three things.

The search and editing models lived in `.ai-models`; the face detector, the
face recogniser and the orientation model lived in the state directory. So
"copy your state folder and your library" left 116 MB of models behind and the
new machine came up with face grouping broken and no obvious reason why.

These pin the two halves of the fix: everything resolves to one folder, and a
household that already has models in the old place has them brought in rather
than downloaded again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ninaivu.media import faces as faces_mod
from ninaivu.media import model_catalog, orientnet


@pytest.fixture(autouse=True)
def leave_the_catalogue_as_it_was():
    """`configure` is process-wide; no test may leak its folder to the next."""
    before = model_catalog._root
    yield
    model_catalog.configure(before)


# -- one folder -------------------------------------------------------------

def test_the_setting_outranks_the_environment(tmp_path, monkeypatch):
    """The environment is one launch; the setting is this installation."""
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "from-the-env"))
    model_catalog.configure(tmp_path / "chosen")
    assert model_catalog.models_root() == tmp_path / "chosen"


def test_an_empty_setting_falls_back_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "from-the-env"))
    model_catalog.configure(None)
    assert model_catalog.models_root() == tmp_path / "from-the-env"


def test_the_default_is_the_folder_beside_the_application(monkeypatch):
    monkeypatch.delenv("NINAIVU_AI_MODELS_DIR", raising=False)
    model_catalog.configure(None)
    assert model_catalog.models_root().name == ".ai-models"


def test_the_face_models_are_where_every_other_model_is(tmp_path):
    model_catalog.configure(tmp_path / "models")
    assert faces_mod.models_dir() == tmp_path / "models"
    # The state directory is accepted and ignored, so that callers holding one
    # did not all have to change in the same breath.
    assert faces_mod.models_dir(tmp_path / "state") == tmp_path / "models"


def test_the_orientation_model_is_too(tmp_path):
    model_catalog.configure(tmp_path / "models")
    orientnet.configure(model_catalog.models_root())
    assert orientnet.model_path().parent == tmp_path / "models"


def test_nothing_resolves_into_the_state_directory_any_more(tmp_path):
    """The bug in one line: a model under `state/models` is a model left behind."""
    model_catalog.configure(tmp_path / "ai")
    orientnet.configure(model_catalog.models_root())
    for path in (faces_mod.models_dir(tmp_path / "state"), orientnet.model_path()):
        assert "state" not in Path(path).parts


# -- bringing the old ones in ----------------------------------------------

def _services(state_dir: Path):
    """A Services object without building one: this needs two attributes."""
    from ninaivu import Services

    blank = Services.__new__(Services)
    blank.cfg = type("Cfg", (), {"state_dir": state_dir})()
    return blank


def test_models_left_in_the_state_directory_are_moved_in(tmp_path):
    state, models = tmp_path / "state", tmp_path / "ai-models"
    (state / "models").mkdir(parents=True)
    (state / "models" / "face_detection_yunet_2023mar.onnx").write_bytes(b"d")
    (state / "models" / "orientation_efficientnetv2s_v2.onnx").write_bytes(b"o")
    models.mkdir()

    _services(state)._gather_models(models)

    assert (models / "face_detection_yunet_2023mar.onnx").read_bytes() == b"d"
    assert (models / "orientation_efficientnetv2s_v2.onnx").read_bytes() == b"o"
    # And the empty folder goes, so it cannot look like models are still there.
    assert not (state / "models").exists()


def test_a_model_already_in_the_new_folder_wins(tmp_path):
    """The spare copy is dropped, not moved over the one in use."""
    state, models = tmp_path / "state", tmp_path / "ai-models"
    (state / "models").mkdir(parents=True)
    (state / "models" / "sface.onnx").write_bytes(b"old")
    models.mkdir()
    (models / "sface.onnx").write_bytes(b"current")

    _services(state)._gather_models(models)

    assert (models / "sface.onnx").read_bytes() == b"current"
    assert not (state / "models" / "sface.onnx").exists()


def test_nothing_to_move_is_not_an_error(tmp_path):
    state, models = tmp_path / "state", tmp_path / "ai-models"
    state.mkdir()
    models.mkdir()
    _services(state)._gather_models(models)          # no folder at all
    (state / "models").mkdir()
    _services(state)._gather_models(models)          # an empty one


def test_the_two_folders_being_the_same_is_left_alone(tmp_path):
    """Somebody may point the setting straight at the old folder."""
    models = tmp_path / "state" / "models"
    models.mkdir(parents=True)
    (models / "sface.onnx").write_bytes(b"keep me")

    _services(tmp_path / "state")._gather_models(models)

    assert (models / "sface.onnx").read_bytes() == b"keep me"


# -- versions ---------------------------------------------------------------
#
# A newer model is a new set of pinned files, and `installed` already checks
# that every file the catalogue names is present at its pinned size. The first
# cut of this recorded a version number in settings.json and assumed 1 for
# anything installed before that existed — which offered every household an
# 80 MB "update" to the orientation model they already had. Caught on a real
# install; these are so it cannot come back.

def _catalogued(tmp_path, monkeypatch, entry):
    model_catalog.configure(tmp_path)
    monkeypatch.setitem(model_catalog.MODELS, "test-model", entry)
    return tmp_path


ENTRY = {
    "label": "A model", "used_for": "testing", "licence": "MIT",
    "source": "nowhere", "runs_on": "CPU", "requires": [],
    "version": 2, "replaces": ["thing_v1.onnx"],
    "files": [{"url": "https://example.invalid/thing_v2.onnx",
               "name": "thing_v2.onnx", "folder": "", "bytes": 4,
               "sha256": "0" * 64}],
}


def test_a_model_that_is_installed_is_at_the_catalogues_version(tmp_path, monkeypatch):
    root = _catalogued(tmp_path, monkeypatch, ENTRY)
    (root / "thing_v2.onnx").write_bytes(b"1234")

    assert model_catalog.installed("test-model") is True
    assert model_catalog.update_available("test-model") is False
    assert model_catalog.describe("test-model")["installed_version"] == 2


def test_the_previous_version_still_on_disk_is_an_update(tmp_path, monkeypatch):
    root = _catalogued(tmp_path, monkeypatch, ENTRY)
    (root / "thing_v1.onnx").write_bytes(b"old")

    assert model_catalog.installed("test-model") is False
    assert model_catalog.update_available("test-model") is True


def test_nothing_on_disk_is_a_first_download_not_an_update(tmp_path, monkeypatch):
    _catalogued(tmp_path, monkeypatch, ENTRY)
    assert model_catalog.installed("test-model") is False
    assert model_catalog.update_available("test-model") is False
    assert model_catalog.describe("test-model")["installed_version"] == 0


def test_both_versions_present_is_not_an_update(tmp_path, monkeypatch):
    """Mid-upgrade, or an interrupted one: the new files decide."""
    root = _catalogued(tmp_path, monkeypatch, ENTRY)
    (root / "thing_v1.onnx").write_bytes(b"old")
    (root / "thing_v2.onnx").write_bytes(b"1234")
    assert model_catalog.update_available("test-model") is False


def test_the_old_file_goes_once_the_new_one_is_there(tmp_path, monkeypatch):
    root = _catalogued(tmp_path, monkeypatch, ENTRY)
    (root / "thing_v1.onnx").write_bytes(b"old")
    (root / "thing_v2.onnx").write_bytes(b"1234")

    assert model_catalog.clear_superseded("test-model") == ["thing_v1.onnx"]
    assert not (root / "thing_v1.onnx").exists()
    assert (root / "thing_v2.onnx").exists()


def test_the_old_file_stays_while_the_new_one_is_missing(tmp_path, monkeypatch):
    """Never delete what is being used on the strength of a download that
    has not finished."""
    root = _catalogued(tmp_path, monkeypatch, ENTRY)
    (root / "thing_v1.onnx").write_bytes(b"old")

    assert model_catalog.clear_superseded("test-model") == []
    assert (root / "thing_v1.onnx").exists()


def test_the_orientation_model_this_household_has_is_not_offered_again():
    """The bug itself, in the shape it was found: v2 on disk, v2 catalogued."""
    entry = model_catalog.MODELS["orientation"]
    assert entry["version"] == 2
    assert entry["files"][0]["name"] == "orientation_efficientnetv2s_v2.onnx"
    # v1 is what it replaces, and is a different filename — so a household
    # with v2 has no superseded file and is offered nothing.
    assert "orientation_efficientnetv2s.onnx" in entry["replaces"]
    assert entry["files"][0]["name"] not in entry["replaces"]
