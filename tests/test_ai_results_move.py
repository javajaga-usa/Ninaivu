"""An index brought to a smaller computer keeps what the bigger one worked out.

Descriptions, search vectors and faces made on the Mac stay and are used;
only new photographs are looked at, by the same models where they are here,
and nothing is thrown away because the models are not (yet).
"""

from types import SimpleNamespace

import pytest

from ninaivu import ai
from ninaivu.media import faces as faces_mod, model_catalog
from ninaivu.media.faceindex import follow_the_index
from ninaivu.media.scanner import AI_VERSION, VECTORS_OWED_KEY, Scanner
from ninaivu.runtime import florence
from ninaivu.server import tiers
from ninaivu.server.config import Config
from ninaivu.storage import db, descriptions

SIGLIP = "ViT-B-16-SigLIP2/webli-squash"


@pytest.fixture()
def index(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    for n in range(3):
        conn.execute(
            "INSERT INTO assets(root, rel_path, folder, filename, ext, kind, size, mtime, thumb, "
            "date_key, visibility, ai_version) VALUES('/lib', ?, '', ?, 'jpg', 'picture', 1, 0, ?, "
            "?, 1, ?)", (f"{n}.jpg", f"{n}.jpg", f"t{n}", f"2026-10-0{n + 1}", AI_VERSION))
    conn.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY id")]
    return conn, ids


# -- descriptions -------------------------------------------------------------

def test_the_base_model_never_redoes_what_large_described(index):
    conn, ids = index
    large = florence.version_of("captions-large")
    descriptions.store(conn, ids[0], "A grandmother lighting a lamp", model="captions-large",
                       version=large)
    base = florence.version_of("captions")
    assert base < large
    owed = [r["id"] for r in descriptions.owed(conn, base)]
    assert ids[0] not in owed and set(owed) == {ids[1], ids[2]}, "only the new photographs"
    assert descriptions.matching_asset_ids(conn, "lamp") == [ids[0]], "and still searched"
    assert descriptions.owed(conn, florence.version_of(None)) == [], "no model: nothing redone"


# -- search vectors -----------------------------------------------------------

def _meta(tmp_path, model):
    conn = db.init_db(tmp_path / "index.db")
    db.set_meta(conn, "ai_model_id", model)
    conn.commit()
    return SimpleNamespace(clip_model="auto", clip_pretrained="", db_path=tmp_path / "index.db")


def test_without_siglip_here_the_older_model_is_not_loaded_over_its_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "installed", lambda name: False)
    cfg = _meta(tmp_path, SIGLIP)
    assert ai.index_search_model(cfg) == SIGLIP
    with pytest.raises(RuntimeError, match="kept"):
        ai.clip_choice(cfg)


def test_an_index_of_the_older_model_or_none_still_gets_it(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "installed", lambda name: False)
    assert ai.clip_choice(_meta(tmp_path, "ViT-B-32/laion2b_s34b_b79k"))[0] == "ViT-B-32"
    no_index = SimpleNamespace(clip_model="auto", clip_pretrained="", db_path=tmp_path / "x.db")
    assert ai.clip_choice(no_index)[0] == "ViT-B-32"
    assert ai.index_search_model(SimpleNamespace()) == ""


def test_the_light_engine_is_used_and_the_vectors_stay(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "installed", lambda name: False)
    monkeypatch.setattr(ai, "onnx_search_ready", lambda cfg: False)
    cfg = _meta(tmp_path, SIGLIP)
    cfg.ai_enabled, cfg.ai_engine, cfg.ai_gpu = True, "clip", False
    monkeypatch.setattr(ai, "_engine", None)
    try:
        engine = ai.build_engine(cfg, force=True)
        assert isinstance(engine, ai.LightEngine)
    finally:
        monkeypatch.setattr(ai, "_engine", None)


class _Light:
    semantic = False
    model_id = "heuristic-v1"


class _Siglip:
    semantic = True
    model_id = SIGLIP


def test_pictures_tagged_while_the_model_was_away_get_vectors_later(cfg, index):
    conn, ids = index
    db.store_embedding(conn, ids[0], SIGLIP, 2, b"\0" * 8)
    scanner = Scanner(cfg)
    scanner.ai = _Light()
    scanner._vectors_owed(conn, tagging=True)                   # noqa: SLF001
    assert db.get_meta(conn, VECTORS_OWED_KEY) == "1"
    scanner.ai = _Siglip()
    scanner._vectors_owed(conn, tagging=False)                  # noqa: SLF001
    versions = dict(conn.execute("SELECT id, ai_version FROM assets").fetchall())
    assert versions[ids[0]] == AI_VERSION, "a picture with its vector is left alone"
    assert versions[ids[1]] == versions[ids[2]] == 0, "the others are tagged again"
    assert db.get_meta(conn, VECTORS_OWED_KEY) == "0", "once"


def test_without_vectors_the_light_engine_owes_nothing(cfg, index):
    conn, _ = index
    scanner = Scanner(cfg)
    scanner.ai = _Light()
    scanner._vectors_owed(conn, tagging=True)                   # noqa: SLF001
    assert db.get_meta(conn, VECTORS_OWED_KEY) is None


def test_the_on_device_search_model_counts_for_the_tier(monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(ai, "onnx_search_ready", lambda cfg: True)
    assert tiers._model_installed(Config())                     # noqa: SLF001
    assert not tiers._model_installed()                         # noqa: SLF001
    monkeypatch.setattr(ai, "onnx_search_ready", lambda cfg: False)
    assert not tiers._model_installed(Config())                 # noqa: SLF001


# -- faces --------------------------------------------------------------------

def _faces(conn, asset_id, model, n):
    for _ in range(n):
        conn.execute(
            "INSERT INTO faces(asset_id, bbox, embedding, model) VALUES(?, '[0,0,1,1]', ?, ?)",
            (asset_id, b"\0" * 8, model))
    conn.commit()


def test_faces_follow_the_index_when_nobody_chose(index):
    conn, ids = index
    _faces(conn, ids[0], faces_mod.ARCFACE_MODEL_ID, 3)
    cfg = Config()
    assert cfg.face_model == "sface"
    assert follow_the_index(cfg, conn) == "arcface"
    assert cfg.face_model == "arcface"
    assert cfg._tier_filled["face_model"] == "arcface", "not saved as the household's choice"  # noqa: SLF001
    assert follow_the_index(cfg, conn) is None, "already"


def test_a_chosen_face_model_stands(index):
    conn, ids = index
    _faces(conn, ids[0], faces_mod.ARCFACE_MODEL_ID, 3)
    cfg = Config()
    cfg._chosen = {"face_model"}                                # noqa: SLF001
    assert follow_the_index(cfg, conn) is None and cfg.face_model == "sface"
    assert follow_the_index(Config(), db.init_db(":memory:")) is None, "no faces: the default"
