"""The second pair of face models (InsightFace's SCRFD and ArcFace), and the
``face_model`` switch that chooses it.

The policy tests need no model: they pin down that each recogniser has its
own thresholds, that a row written by one recogniser is never read as the
other's, and that switching sends the next face pass over every photograph
while confirmed names survive. The engine tests need the two ONNX files
(``buffalo_l/det_10g.onnx`` and ``w600k_r50.onnx`` in the models folder, as
the AI models page installs them) and are skipped without them.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pytest

from ninaivu.media import facematch as fm, faces as faces_mod, model_catalog
from ninaivu.storage import db


# ---------------------------------------------------------------------------
# Choosing a pair
# ---------------------------------------------------------------------------

def test_each_recogniser_has_its_own_thresholds():
    sface = fm.thresholds_for(faces_mod.MODEL_ID)
    arc = fm.thresholds_for(faces_mod.ARCFACE_MODEL_ID)
    assert sface == fm.SFACE and sface.auto == fm.T_AUTO and sface.link == fm.T_LINK
    assert arc == fm.ARCFACE and arc is not sface
    # The same shape of policy: asking before deciding, a link floor below
    # the cluster floor, an auto bar above everything.
    for rules in (sface, arc):
        assert rules.suggest < rules.link < rules.cluster < rules.auto
        assert 0 < rules.margin < 0.2
    # A row from before the setting existed carries no model worth knowing.
    assert fm.thresholds_for(None) == fm.SFACE
    assert fm.thresholds_for("something-else") == fm.SFACE


def test_the_setting_names_a_model_and_its_width():
    assert faces_mod.model_id_for("sface") == faces_mod.MODEL_ID
    assert faces_mod.model_id_for("ArcFace") == faces_mod.ARCFACE_MODEL_ID
    assert faces_mod.model_id_for(None) == faces_mod.MODEL_ID
    assert faces_mod.model_id_for("nonsense") == faces_mod.MODEL_ID
    assert faces_mod.embed_dim_of(faces_mod.MODEL_ID) == 128
    assert faces_mod.embed_dim_of(faces_mod.ARCFACE_MODEL_ID) == 512


def test_one_recognisers_row_is_never_read_as_the_others():
    """SFace's 128 values and ArcFace's 512 are not comparable: a matcher
    that asked for one must never be handed the other."""
    short = np.ones(128, dtype="float32").tobytes()
    long = np.ones(512, dtype="float32").tobytes()
    assert faces_mod.unpack(short, 128).shape == (128,)
    assert faces_mod.unpack(long, 512).shape == (512,)
    assert faces_mod.unpack(short, 512) is None
    assert faces_mod.unpack(long, 128) is None
    # Without a width named, any width a known model writes is fine, and
    # nothing else is.
    assert faces_mod.unpack(long).shape == (512,)
    assert faces_mod.unpack(np.ones(77, dtype="float32").tobytes()) is None
    assert faces_mod.unpack_many([short, long, short], 128).shape == (2, 128)
    assert faces_mod.unpack_many([short, long], 512).shape == (1, 512)


def test_the_arcface_engine_reports_what_is_missing_rather_than_raising(cfg):
    engine = faces_mod.FaceEngine(cfg.state_dir / "nowhere", model="arcface")
    assert engine.available is False
    assert engine.model == "arcface"
    assert engine.model_id == faces_mod.ARCFACE_MODEL_ID
    assert engine.embed_dim == 512
    assert engine.info["choice"] == "arcface"
    assert engine.detect(None) == []
    status = faces_mod.model_status(cfg.state_dir, "arcface")
    assert status["choice"] == "arcface" and status["ready"] is False
    assert {Path(m["file"]).name for m in status["models"].values()} == {
        "det_10g.onnx", "w600k_r50.onnx"}


def test_the_default_engine_is_still_sface(cfg):
    engine = faces_mod.FaceEngine(cfg.state_dir / "nowhere")
    assert engine.model == "sface" and engine.model_id == faces_mod.MODEL_ID
    assert engine.embed_dim == 128


def test_switching_the_setting_sends_the_next_pass_over_every_photograph(scanned):
    """The other recogniser's rows are not comparable, so every photograph
    is looked at again; the faces there stay until its turn comes."""
    cfg, conn, _ = scanned
    conn.execute("UPDATE assets SET face_version=?", (faces_mod.FACE_VERSION,))
    conn.commit()
    assert db.assets_needing_faces(conn, cfg.active_root, faces_mod.FACE_VERSION) == []
    touched = db.reset_face_pass(conn)
    assert touched > 0
    assert len(db.assets_needing_faces(conn, cfg.active_root, faces_mod.FACE_VERSION)) == touched
    # Done once: a second reset has nothing left to touch.
    assert db.reset_face_pass(conn) == 0


def test_a_kept_indexer_is_made_again_when_the_setting_changes(scanned):
    """Audit C-02: the scan kept its SFace indexer after the console chose
    ArcFace, and looked at the whole library again with the old pair."""
    from ninaivu.media import scanner as scanner_mod
    cfg, _, _ = scanned
    scan = scanner_mod.Scanner(cfg)
    first = scan._face_indexer()
    assert first is not None and first.is_current()
    assert scan._face_indexer() is first, "kept while the setting stands"
    cfg.face_model = "arcface"
    try:
        assert not first.is_current()
        second = scan._face_indexer()
        assert second is not first and second.engine.model == "arcface"
    finally:
        cfg.face_model = "sface"


def test_a_worker_without_the_models_leaves_the_photograph_owed(cfg, tmp_path):
    """Audit C-02: raising charged every photograph a failure, and two set
    it aside as having no faces."""
    from PIL import Image
    from ninaivu.engine import work
    picture = tmp_path / "one.jpg"
    Image.new("RGB", (64, 64)).save(picture)
    assert work.find_faces(str(tmp_path / "no-models"), str(picture), 0, "arcface") == work.AGAIN
    assert ("faces", str(tmp_path / "no-models"), "arcface") not in work._kept()


def test_the_table_says_which_recogniser_made_most_of_it(scanned):
    cfg, conn, _ = scanned
    assert db.face_model_in_use(conn) is None
    pictures = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND trashed=0 LIMIT 3")]
    assert len(pictures) == 3
    row = {"bbox": [0, 0, 90, 90], "landmarks": [], "det_score": 0.9,
           "sharpness": 40.0, "quality": 0.8}
    for asset in pictures[:2]:
        db.replace_asset_faces(conn, asset, [dict(row, embedding=np.zeros(512, "float32").tobytes())],
                               faces_mod.ARCFACE_MODEL_ID)
    db.replace_asset_faces(conn, pictures[2], [dict(row, embedding=np.zeros(128, "float32").tobytes())],
                           faces_mod.MODEL_ID)
    assert db.face_model_in_use(conn) == faces_mod.ARCFACE_MODEL_ID


def test_the_setting_is_offered_with_its_two_values(cfg):
    from ninaivu.server import settings_groups
    assert settings_groups.CHOICES["face_model"] == ("sface", "arcface")
    assert cfg.face_model == "sface"
    with pytest.raises(settings_groups.BadValue):
        settings_groups.apply(cfg, {"face_model": "dlib"})
    assert settings_groups.apply(cfg, {"face_model": "ArcFace"}) == ["face_model"]
    assert cfg.face_model == "arcface"


# ---------------------------------------------------------------------------
# The catalogue entry
# ---------------------------------------------------------------------------

def test_the_catalogue_pins_the_pack_and_takes_two_files_from_it():
    entry = model_catalog.MODELS["faces-arcface"]
    assert "non-commercial" in entry["licence"]
    assert entry["requires"] == ["onnxruntime"]
    (archive,) = entry["files"]
    assert archive["name"].endswith(".zip") and archive["sha256"]
    assert set(archive["take"]) == {"det_10g.onnx", "w600k_r50.onnx"}
    kept = {path.name for path, _ in model_catalog.kept_files(archive)}
    assert kept == {"det_10g.onnx", "w600k_r50.onnx"}
    assert not model_catalog.installed("faces-arcface")


def test_a_zip_archive_is_unpacked_by_its_pinned_members(tmp_path, monkeypatch):
    """Only the members named, at the sizes pinned; a member of another size
    is refused, and nothing in the archive decides where anything lands."""
    monkeypatch.setattr(model_catalog, "models_root", lambda: tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("det.onnx", b"d" * 10)
        bundle.writestr("rec.onnx", b"r" * 20)
        bundle.writestr("../escape.onnx", b"x")
    archive = tmp_path / "pack.zip"
    archive.write_bytes(buffer.getvalue())
    entry = {"name": "pack.zip", "folder": "pack",
             "take": {"detector.onnx": {"member": "det.onnx", "bytes": 10},
                      "recogniser.onnx": {"member": "rec.onnx", "bytes": 20}}}
    (tmp_path / "pack").mkdir()
    model_catalog._unpack(entry, archive)
    assert (tmp_path / "pack" / "detector.onnx").read_bytes() == b"d" * 10
    assert (tmp_path / "pack" / "recogniser.onnx").read_bytes() == b"r" * 20
    assert sorted(p.name for p in (tmp_path / "pack").iterdir()) == ["detector.onnx", "recogniser.onnx"]

    wrong = {"name": "pack.zip", "folder": "pack",
             "take": {"detector.onnx": {"member": "det.onnx", "bytes": 11}}}
    with pytest.raises(model_catalog.ChecksumMismatch):
        model_catalog._unpack(wrong, archive)
    missing = {"name": "pack.zip", "folder": "pack",
               "take": {"x.onnx": {"member": "nothing.onnx", "bytes": 1}}}
    with pytest.raises(model_catalog.ChecksumMismatch):
        model_catalog._unpack(missing, archive)


# ---------------------------------------------------------------------------
# The engine, when the models are present
# ---------------------------------------------------------------------------

def _arcface_engine(cfg):
    """The ArcFace engine from the test state dir, the real models folder or
    ``NINAIVU_TEST_ARCFACE`` (a folder holding the two files)."""
    import os
    candidates = [cfg.state_dir, Path.home() / ".ninaivu"]
    engine = None
    for state in candidates:
        engine = faces_mod.FaceEngine(state, model="arcface")
        if engine.available:
            return engine
    folder = os.environ.get("NINAIVU_TEST_ARCFACE")
    if folder:
        from ninaivu.media import faces_arcface
        pair = faces_arcface.Recogniser(Path(folder), threads=2)
        engine = faces_mod.FaceEngine(cfg.state_dir, model="arcface")
        engine._detector = engine._recognizer = pair
        engine.min_det_score = faces_arcface.MIN_DET_SCORE
        engine.unavailable_reason = None
        engine._waiting_for_files = False
        return engine
    pytest.skip(f"ArcFace models unavailable: {engine.unavailable_reason}")


def test_arcface_finds_the_test_face_and_tells_it_from_its_mirror_image(cfg):
    import cv2
    engine = _arcface_engine(cfg)
    assert engine.available and engine.embed_dim == 512
    image = cv2.imread(str(Path(__file__).parent / "data" / "face.jpg"))
    found = engine.detect(image)
    assert len(found) == 1
    face = found[0]
    assert face.embedding.shape == (512,)
    assert np.isclose(float(np.linalg.norm(face.embedding)), 1.0)
    assert face.crop.shape == (112, 112, 3)
    assert len(face.landmarks) == 5 and 0 < face.quality <= 1
    # The same face, mirrored and at a third of the size, is plainly the same
    # person to this recogniser, well above its auto bar.
    rules = fm.thresholds_for(engine.model_id)
    mirrored = engine.detect(cv2.flip(image, 1))[0]
    small = engine.detect(cv2.resize(image, None, fx=0.34, fy=0.34, interpolation=cv2.INTER_AREA))[0]
    assert fm.similarity(face.embedding, mirrored.embedding) > rules.auto
    assert fm.similarity(face.embedding, small.embedding) > rules.auto
    # And nothing in a plain colour.
    assert engine.detect(np.full((480, 640, 3), 128, dtype="uint8")) == []
    # Where every face is, for retouching, at a lower bar.
    located = engine.locate(image)
    assert len(located) == 1 and len(located[0]["landmarks"]) == 5


def test_arcface_tells_the_test_face_from_a_drawn_stranger(cfg):
    """A face drawn from a recipe is a face to the detector and a stranger
    to the recogniser: it must land below the suggest floor."""
    import cv2
    from synthetic_faces import draw
    engine = _arcface_engine(cfg)
    real = engine.detect(cv2.imread(str(Path(__file__).parent / "data" / "face.jpg")))[0]
    drawn = draw([dict(x=360, y=260, d=90, skin="brown", hair="black")], size=(720, 560))
    strangers = engine.detect(np.ascontiguousarray(drawn.rgb[:, :, ::-1]).astype("uint8"))
    if not strangers:
        pytest.skip("the detector does not take the drawing for a face")
    rules = fm.thresholds_for(engine.model_id)
    assert fm.similarity(real.embedding, strangers[0].embedding) < rules.suggest
