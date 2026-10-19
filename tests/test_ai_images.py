"""The on-device AI engine's image models (2.0, proposal C, step 2): search
without PyTorch (SigLIP 2 through ONNX Runtime) and a description of every
photograph (Florence-2), kept, searched and shown.

The real models are 2 GB and are not downloaded here: they run on stand-in
ONNX files with the same inputs and outputs (tests/onnx_fakes.py), which ONNX
Runtime loads and runs exactly as it would the real ones. The real models are
checked by .github/workflows/ai-engine.yml, against PyTorch's vectors.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from ninaivu import ai
from ninaivu.media import model_catalog
from ninaivu.runtime import describer as describer_mod
from ninaivu.runtime import florence, siglip
from ninaivu.runtime.describer import Describer
from ninaivu.runtime.host import HostCrashed, ModelHost, Unavailable
from ninaivu.storage import db, descriptions

needs_onnx = pytest.mark.skipif(
    any(__import__("importlib").util.find_spec(m) is None
        for m in ("onnx", "onnxruntime", "tokenizers")),
    reason="ONNX Runtime, onnx and tokenizers make and run the stand-in models")


# ---------------------------------------------------------------------------
# Search: photographs and words as open_clip hands them to SigLIP 2
# ---------------------------------------------------------------------------

def test_words_are_cleaned_as_open_clip_cleans_them_for_siglip():
    assert siglip.clean("  Grandma's  birthday_cake!! ") == "grandmas birthday cake"
    assert siglip.clean("Tom &amp; Jerry") == "tom jerry"


class _Tokens:
    """Gemma's tokenizer, as far as this needs it: a word a token."""

    def encode(self, text, add_special_tokens=True):
        assert add_special_tokens is False, "no beginning-of-text for SigLIP 2"
        return SimpleNamespace(ids=[10 + len(w) for w in text.split()])


def test_the_tokens_end_with_end_of_text_and_are_padded_to_sixty_four():
    ids = siglip.token_ids(_Tokens(), "A dog, on the beach")
    assert len(ids) == 64
    assert ids[:6] == [11, 13, 12, 13, 15, siglip.EOS]
    assert set(ids[6:]) == {siglip.PAD}
    long = siglip.token_ids(_Tokens(), "word " * 100)
    assert len(long) == 64 and long[-1] == siglip.EOS, "cut, and still ended"


def test_a_photograph_is_squashed_to_the_models_square_and_scaled():
    image = Image.new("RGB", (640, 200), (255, 0, 128))
    square = siglip.pixels(image)
    assert square.shape == (224, 224, 3) and square.dtype == np.uint8
    batch = siglip.normalise(np.stack([square]))
    assert batch.shape == (1, 3, 224, 224)
    assert np.allclose(batch[0, 0], 1.0) and np.allclose(batch[0, 1], -1.0)
    assert batch.flags["C_CONTIGUOUS"]


def test_tags_and_the_content_check_are_worked_out_as_the_pytorch_engine_does():
    rng = np.random.default_rng(3)
    concepts = siglip.unit(rng.normal(size=(len(ai.FLAT_CONCEPTS), 16)))
    nsfw = siglip.unit(rng.normal(size=(len(ai.NSFW_PROMPTS), 16)))
    vector = siglip.unit(concepts[5] + 0.01 * rng.normal(size=16))[None]
    [result] = siglip.score(vector, {"concepts": concepts, "nsfw": nsfw},
                            tag_threshold=0.5, max_tags=8)
    assert result["tags"][0] == ai.LABELS[5]
    logits = np.exp(siglip.LOG_SCALE) * (vector @ nsfw[ai.NSFW_UNSAFE_FROM:].T) + siglip.BIAS
    assert result["nsfw_score"] == round(float((1 / (1 + np.exp(-logits))).max()), 4)


@needs_onnx
def test_the_model_makes_unit_vectors_and_keeps_the_tag_descriptions(tmp_path, monkeypatch):
    import onnx_fakes
    folder = onnx_fakes.siglip(tmp_path / "siglip2-onnx", words=["a", "dog"])
    model = siglip.Siglip(folder)
    image = Image.new("RGB", (300, 200), (200, 30, 10))
    vectors = model.embed_pixels(np.stack([siglip.pixels(image)] * 3))
    assert vectors.shape == (3, onnx_fakes.DIM)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    words = model.embed_texts(["a dog", "A DOG!", "a cat"])
    assert np.allclose(words[0], words[1]), "the same once cleaned"
    monkeypatch.setattr(ai, "FLAT_CONCEPTS", ["dog", "beach"])
    monkeypatch.setattr(ai, "LABELS", ["dog", "beach"])
    first = model.prompts()
    kept = list(folder.glob("prompts-*.npz"))
    assert len(kept) == 1 and first["concepts"].shape == (2, onnx_fakes.DIM)
    again = siglip.Siglip(folder)
    monkeypatch.setattr(again, "embed_texts", lambda *a, **k: pytest.fail("made again"))
    assert np.allclose(again.prompts()["concepts"], first["concepts"]), "read back, not made"


@needs_onnx
def test_the_model_host_answers_for_photographs_and_words(tmp_path):
    import onnx_fakes
    folder = onnx_fakes.siglip(tmp_path / "models", words=["a", "dog"])
    host = ModelHost({"models": str(folder), "threads": 1,
                      "capabilities": {"search": "runtime_fakes:SIGLIP"}})
    try:
        image = Image.new("RGB", (64, 64), (10, 200, 10))
        answers = host.call("search", op="images", timeout=120,
                            pixels=[siglip.pixels(image)], tag_threshold=-1.0, max_tags=3)
        assert len(answers) == 1 and len(answers[0]["tags"]) == 3
        assert len(answers[0]["embedding"]) == 4 * onnx_fakes.DIM
        assert 0.0 <= answers[0]["nsfw_score"] <= 1.0
        texts = host.call("search", op="texts", texts=["a dog"], timeout=120)
        assert texts.shape == (1, onnx_fakes.DIM)
    finally:
        host.close()


def test_without_its_model_search_cannot_load(tmp_path, monkeypatch):
    model_catalog.configure(tmp_path / "models")
    try:
        monkeypatch.setattr(siglip, "missing_packages", lambda: None)
        assert "not downloaded" in siglip.ready()
        with pytest.raises(Unavailable):
            siglip._load({})
    finally:
        model_catalog.configure(None)


# ---------------------------------------------------------------------------
# Search: the server's half
# ---------------------------------------------------------------------------

class _SearchHost:
    def __init__(self):
        self.calls = []

    def call(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        assert capability == "search"
        if kwargs["op"] == "images":
            return [{"tags": ["beach", "sunset"], "nsfw_score": 0.001,
                     "embedding": np.full(4, 0.5, np.float32).tobytes()}
                    for _ in kwargs["pixels"]]
        if kwargs["op"] == "texts":
            return np.ones((len(kwargs["texts"]), 4), np.float32) / 2
        return np.ones((len(kwargs["pixels"]), 4), np.float32) / 2

    def close(self):
        self.calls.append(("closed", {}))


def test_the_engine_opens_photographs_here_and_asks_the_host_for_the_rest(tmp_path):
    photo = tmp_path / "beach.jpg"
    Image.new("RGB", (900, 300), (240, 240, 240)).save(photo)
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a picture")
    host = _SearchHost()
    engine = ai.OnnxEngine(SimpleNamespace(), host=host)
    assert engine.semantic and engine.model_id == "ViT-B-16-SigLIP2/webli-squash"
    assert engine.reads_ahead
    bound = ai.bind(engine, SimpleNamespace(tag_threshold=0.18, max_tags=6))
    first, second = bound.analyse([photo, broken])
    assert first["tags"][:2] == ["beach", "sunset"] and "wide" in first["tags"], \
        "the model's tags, then the picture's own"
    assert first["caption"] == "beach, sunset, wide" and first["nsfw_score"] == 0.001
    assert len(first["embedding"]) == 16
    assert second == {"tags": []}, "an unreadable picture is not a failed batch"
    _, sent = host.calls[0]
    assert len(sent["pixels"]) == 1 and sent["pixels"][0].shape == (224, 224, 3)
    assert sent["tag_threshold"] == 0.085, "SigLIP's own threshold, as on PyTorch"
    assert engine.encode_text("beach").shape == (4,)
    assert engine.embed_images([Image.new("RGB", (10, 10))]).shape == (1, 4)
    engine.close()


def test_with_the_engine_on_and_ready_search_runs_in_it(monkeypatch):
    monkeypatch.setattr(ai, "_engine", None)
    monkeypatch.setattr(ai, "onnx_search_ready", lambda cfg: True)
    monkeypatch.setattr(ai, "ClipEngine", lambda *a, **k: pytest.fail("PyTorch loaded"))
    cfg = SimpleNamespace(ai_enabled=True, ai_engine="clip", ai_runtime=True)
    engine = ai.build_engine(cfg, force=True)
    assert isinstance(engine, ai.OnnxEngine)
    host = _SearchHost()
    engine._host = host
    monkeypatch.setattr(ai, "onnx_search_ready", lambda cfg: False)
    assert isinstance(ai.build_engine(SimpleNamespace(ai_enabled=False, ai_engine="clip"),
                                      force=True), ai.Engine)
    assert host.calls[-1][0] == "closed", "a replaced engine's host goes with it"
    monkeypatch.setattr(ai, "_engine", None)


def test_the_engine_is_ready_only_when_switched_on(monkeypatch):
    monkeypatch.setattr(siglip, "ready", lambda: None)
    assert ai.onnx_search_ready(SimpleNamespace(ai_runtime=True))
    assert not ai.onnx_search_ready(SimpleNamespace(ai_runtime=False))


# ---------------------------------------------------------------------------
# Descriptions: Florence-2
# ---------------------------------------------------------------------------

def test_a_photograph_is_prepared_as_florence2processor_prepares_it():
    batch = florence.pixels(Image.new("RGB", (640, 480), (255, 255, 255)))
    assert batch.shape == (1, 3, 768, 768) and batch.dtype == np.float32
    assert np.allclose(batch[0, 0], (1 - 0.485) / 0.229, atol=1e-5)


def test_three_tokens_are_never_written_twice():
    assert florence.banned([5, 6, 7, 5, 6]) == {7}
    assert florence.banned([5, 6, 7, 8]) == set()
    assert florence.banned([5]) == set()
    assert florence.tidy("<s> A dog   on the beach.</s><pad>") == "A dog on the beach."


@needs_onnx
def test_the_decoder_writes_a_description_a_token_at_a_time(tmp_path):
    import onnx_fakes
    folder = onnx_fakes.florence(tmp_path / "florence-2-base")
    model = florence.Florence(folder)
    assert model.describe(Image.new("RGB", (640, 480), (90, 120, 200))) == "a dog on the beach", \
        "forced to begin, then the sentence, then stopped at end-of-text"


@needs_onnx
def test_a_sentence_that_would_repeat_itself_is_cut_short(tmp_path):
    import onnx_fakes
    folder = onnx_fakes.florence(tmp_path / "florence-2-base",
                                 sentence=["a", "dog", "a", "dog", "a", "dog", "a", "dog"])
    text = florence.Florence(folder).describe(Image.new("RGB", (64, 64)))
    assert "a dog a dog a" not in text and len(text.split()) < 12


@needs_onnx
def test_the_host_describes_each_picture_and_one_that_will_not_open_is_its_own_failure(tmp_path):
    import onnx_fakes
    folder = onnx_fakes.florence(tmp_path / "models")
    photo = tmp_path / "p.webp"
    Image.new("RGB", (640, 427), (200, 100, 50)).save(photo)
    host = ModelHost({"models": str(folder), "threads": 1,
                      "capabilities": {"describe": "runtime_fakes:FLORENCE"}})
    try:
        good, bad = host.call("describe", paths=[str(photo), str(tmp_path / "gone.webp")],
                              timeout=120)
    finally:
        host.close()
    assert good["text"] == "a dog on the beach" and good["took"] >= 0
    assert "error" in bad


# ---------------------------------------------------------------------------
# The pinned downloads
# ---------------------------------------------------------------------------

def test_both_models_are_pinned_to_a_revision_and_their_bytes():
    for model_id in (siglip.MODEL, *florence.MODELS):
        model = model_catalog.MODELS[model_id]
        assert model["requires"] == ["onnxruntime", "tokenizers"]
        for entry in model["files"]:
            assert re.fullmatch(r"[0-9a-f]{40}", entry["revision"])
            assert entry.get("sha256") or entry.get("git_sha1")
    for model_id, expected in (("captions", "florence-2-base"),
                               ("captions-large", "florence-2-large")):
        files = model_catalog.MODELS[model_id]["files"]
        assert {f["name"] for f in files} == set(florence.PARTS[model_id].values())
        assert {f["folder"] for f in files} == {expected}
    assert {f["git_sha1"] for m in florence.MODELS for f in model_catalog.MODELS[m]["files"]
            if f["name"] == "tokenizer.json"} == {"c6919f318f277e660b557212ed1edaf28b037fc8"}
    tokenizer = next(f for f in model_catalog.MODELS[siglip.MODEL]["files"]
                     if f["name"].endswith("tokenizer.json"))
    assert tokenizer["sha256"] == next(
        f["sha256"] for f in model_catalog.MODELS["siglip2"]["files"]
        if f["name"] == "tokenizer.json"), "the very tokenizer PyTorch's SigLIP 2 reads"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_file_pinned_by_its_git_blob_id_is_checked_by_it(tmp_path):
    model_catalog.configure(tmp_path / "models")
    try:
        blob = b'{"model": "a tokenizer"}\n'
        git = hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest()
        entry = {"url": "https://example.invalid/tokenizer.json", "name": "tokenizer.json",
                 "folder": "pinned", "bytes": len(blob), "git_sha1": git}
        path = model_catalog.download_file(entry, opener=lambda *a, **k: _Response(blob))
        assert path.read_bytes() == blob
        with pytest.raises(model_catalog.ChecksumMismatch):
            model_catalog.download_file({**entry, "git_sha1": "0" * 40},
                                        opener=lambda *a, **k: _Response(blob))
        with pytest.raises(model_catalog.ChecksumMismatch):
            model_catalog.download_file(entry, opener=lambda *a, **k: _Response(blob + b" "))
    finally:
        model_catalog.configure(None)


# ---------------------------------------------------------------------------
# Kept, owed and searched
# ---------------------------------------------------------------------------

@pytest.fixture()
def indexed(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    for n, (kind, day, visibility) in enumerate([
            ("picture", "2026-10-01", 1), ("picture", "2026-10-03", 1), ("video", "2026-10-02", 1),
            ("audio", "2026-10-04", 1), ("picture", "2026-10-05", 2)]):
        conn.execute(
            "INSERT INTO assets(root, rel_path, folder, filename, ext, kind, size, mtime, thumb, "
            "date_key, visibility) VALUES('/lib', ?, '', ?, 'jpg', ?, 1, 0, ?, ?, ?)",
            (f"{n}.jpg", f"{n}.jpg", kind, f"t{n}", day, visibility))
    conn.commit()
    ids = [r[0] for r in conn.execute("SELECT id FROM assets ORDER BY id")]
    return conn, ids


def test_pictures_and_videos_are_owed_newest_first_and_hidden_ones_never(indexed):
    conn, ids = indexed
    owed = [r["id"] for r in descriptions.owed(conn, 1)]
    assert owed == [ids[1], ids[2], ids[0]], "no sound file, nothing hidden from AI"
    assert descriptions.count_owed(conn, 1) == 3
    page = descriptions.owed(conn, 1, limit=2)
    rest = descriptions.owed(conn, 1, limit=2, before=(page[-1]["date_key"], page[-1]["id"]))
    assert [r["id"] for r in page + rest] == owed, "read a page at a time, each once"


def test_a_description_is_kept_found_and_made_again_by_a_newer_model(indexed):
    conn, ids = indexed
    assert descriptions.store(conn, ids[0], "A grandmother lighting a lamp at a temple",
                              model="captions", version=1)
    assert descriptions.get(conn, ids[0]) == "A grandmother lighting a lamp at a temple"
    assert [r["id"] for r in descriptions.owed(conn, 1)] == [ids[1], ids[2]]
    assert [r["id"] for r in descriptions.owed(conn, 2)][-1] == ids[0]
    assert descriptions.matching_asset_ids(conn, "lamp grandmother") == [ids[0]]
    assert descriptions.matching_asset_ids(conn, "grand") == [ids[0]], "the start of a word"
    assert descriptions.matching_asset_ids(conn, "lamp beach") == []
    assert not descriptions.store(conn, ids[4], "hidden", model="captions", version=1), \
        "nothing written for an item hidden from AI"
    descriptions.store(conn, ids[0], "A cat on a sofa", model="captions", version=2)
    assert descriptions.matching_asset_ids(conn, "lamp") == [], "the index follows the text"
    conn.execute("DELETE FROM assets WHERE id=?", (ids[0],))
    conn.commit()
    assert descriptions.matching_asset_ids(conn, "cat") == [] and descriptions.count(conn) == 0


def test_a_picture_that_fails_twice_is_set_aside_until_a_newer_model(indexed):
    conn, ids = indexed
    assert not descriptions.fail(conn, ids[1], 1, "will not open")
    assert ids[1] in [r["id"] for r in descriptions.owed(conn, 1)], "tried again once"
    assert descriptions.fail(conn, ids[1], 1, "will not open")
    assert ids[1] not in [r["id"] for r in descriptions.owed(conn, 1)]
    assert ids[1] in [r["id"] for r in descriptions.owed(conn, 2)]
    assert descriptions.get(conn, ids[1]) is None


def test_hiding_an_item_takes_its_description_back(indexed):
    conn, ids = indexed
    descriptions.store(conn, ids[0], "A family at the beach", model="captions", version=1)
    conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (ids[0],))
    conn.commit()
    assert db.forget_ai_reading(conn, ids=[ids[0]]) == 1
    assert descriptions.get(conn, ids[0]) is None
    assert descriptions.matching_asset_ids(conn, "beach") == []


# ---------------------------------------------------------------------------
# Describing in the background
# ---------------------------------------------------------------------------

class _DescribeHost:
    def __init__(self, answer=None, model="captions"):
        self.answer = answer
        self.model = model
        self.calls = []
        self.recycled = 0

    def call(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return [{"text": f"photo {Path(p).name}", "model": self.model}
                if "t1_" not in p else {"error": "bad"} for p in kwargs["paths"]]

    def recycle(self):
        self.recycled += 1


def _describer(indexed, tmp_path, monkeypatch, answer=None, *, ready=None, chosen="captions"):
    conn, ids = indexed
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir(exist_ok=True)
    for n in range(5):
        (thumbs / f"t{n}_640.webp").write_bytes(b"x")
    monkeypatch.setattr(describer_mod, "ready", lambda: ready)
    monkeypatch.setattr(florence, "chosen_model", lambda: chosen)
    cfg = SimpleNamespace(ai_runtime=True, thumbs_dir=thumbs, thumb_sizes=(256, 640),
                          thumb_format="WEBP")
    host = _DescribeHost(answer)
    return Describer(cfg, lambda: conn, host=lambda: host, hold=lambda: None), host


def test_the_engine_describes_every_picture_owed_from_its_preview(indexed, tmp_path, monkeypatch):
    conn, ids = indexed
    describer, host = _describer(indexed, tmp_path, monkeypatch)
    state = describer.run()
    assert state["done"] == 2 and state["failed"] == 1 and state["owed"] == 1
    capability, call = host.calls[0]
    assert capability == "describe" and all(p.endswith("_640.webp") for p in call["paths"])
    assert descriptions.get(conn, ids[0]) == "photo t0_640.webp"
    assert descriptions.get(conn, ids[2]) == "photo t2_640.webp"
    host.calls.clear()
    describer.run()
    assert [len(c["paths"]) for _, c in host.calls] == [1], "only the one that failed, once more"


def test_a_better_model_downloaded_meanwhile_is_loaded_and_describes_them_again(
        indexed, tmp_path, monkeypatch):
    conn, ids = indexed
    describer, host = _describer(indexed, tmp_path, monkeypatch, chosen="captions-large")
    describer.run()
    assert host.recycled == 1, "the host still had base loaded: started afresh"
    made = conn.execute("SELECT model, version FROM descriptions WHERE asset_id=?",
                        (ids[0],)).fetchone()
    assert tuple(made) == ("captions", florence.version_of("captions"))
    assert ids[0] in [r["id"] for r in descriptions.owed(conn, florence.version())], \
        "owed again, for large"
    host.model = "captions-large"
    host.recycled = 0
    describer.run()
    assert host.recycled == 0
    assert descriptions.count_owed(conn, florence.version()) == 0, "all by large, or set aside"


def test_large_is_used_when_downloaded_and_suggested_from_24_gb(tmp_path, monkeypatch):
    installed = set()
    monkeypatch.setattr(model_catalog, "installed", lambda m: m in installed)
    assert florence.chosen_model() is None and florence.version() == 0
    installed.add("captions")
    assert florence.chosen_model() == "captions"
    installed.add("captions-large")
    assert florence.chosen_model() == "captions-large"
    assert florence.version_of("captions-large") > florence.version_of("captions") > 0
    assert florence.suggested_model(48 * 1024 ** 3) == "captions-large"
    assert florence.suggested_model(16 * 1024 ** 3) == "captions"
    with pytest.raises(Unavailable):
        monkeypatch.setattr(florence, "ready", lambda: None)
        florence._load({"describe_model": "captions-huge"})


def test_a_crash_is_counted_against_each_picture_of_its_batch(indexed, tmp_path, monkeypatch):
    conn, _ = indexed
    describer, _ = _describer(indexed, tmp_path, monkeypatch, HostCrashed("gone"))
    assert describer.run()["failed"] == 3
    assert {r[0] for r in conn.execute("SELECT error FROM descriptions")} == {"gone"}


def test_a_missing_model_ends_the_pass_and_blames_no_picture(indexed, tmp_path, monkeypatch):
    conn, _ = indexed
    describer, _ = _describer(indexed, tmp_path, monkeypatch, Unavailable("no model"))
    state = describer.run()
    assert state["message"] == "no model" and state["failed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0


def test_not_ready_asks_nothing_and_says_why(indexed, tmp_path, monkeypatch):
    describer, host = _describer(indexed, tmp_path, monkeypatch,
                                 ready="the description model is not downloaded")
    assert describer.run()["message"] == "the description model is not downloaded"
    assert host.calls == []


def test_switched_off_the_pass_stops(indexed, tmp_path, monkeypatch):
    describer, host = _describer(indexed, tmp_path, monkeypatch)
    describer.cfg.ai_runtime = False
    describer.run()
    assert host.calls == []


# ---------------------------------------------------------------------------
# In the app
# ---------------------------------------------------------------------------

def test_a_description_is_searched_and_shown_to_who_may_open_the_photograph(as_family, people):
    found = as_family.get("/api/assets?plain=1").get_json()["items"]
    asset_id = found[0]["id"]
    assert as_family.get(f"/api/asset/{asset_id}").get_json()["description"] is None
    descriptions.store(people["conn"], asset_id, "Grandmother lighting a kuthuvilakku",
                       model="captions", version=1)
    hits = as_family.get("/api/assets?q=kuthuvilakku&plain=1").get_json()["items"]
    assert [i["id"] for i in hits] == [asset_id]
    assert as_family.get(f"/api/asset/{asset_id}").get_json()["description"] == \
        "Grandmother lighting a kuthuvilakku"


def test_the_drive_copy_of_the_index_carries_no_descriptions(scanned, tmp_path):
    import sqlite3
    import tarfile
    from ninaivu.cloud import index_copy
    cfg, conn, _ = scanned
    asset_id = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    descriptions.store(conn, asset_id, "Thatha reading the Hindu", model="captions", version=1)
    bundle = index_copy.make_bundle(cfg.state_dir, tmp_path / "out")
    with tarfile.open(bundle) as tar:
        member = next(n for n in tar.getnames() if n.endswith("index.db"))
        tar.extract(member, tmp_path / "x")
    index = next((tmp_path / "x").rglob("index.db"))
    copied = sqlite3.connect(str(index))
    assert copied.execute("SELECT COUNT(*) FROM descriptions").fetchone()[0] == 0
    copied.close()


def test_the_models_page_says_what_the_engine_does_with_them(as_admin):
    payload = as_admin.get("/api/admin/ai-models").get_json()
    assert {siglip.MODEL, florence.MODEL} <= {m["id"] for m in payload["models"]}
    engine = payload["engine"]
    assert engine["enabled"] is False and engine["descriptions"]["made"] == 0
    assert engine["descriptions"]["owed"] > 0, "the scanned photographs are owed"


def test_the_extra_is_present_only_with_both_packages(monkeypatch):
    from ninaivu.media import components
    entry = components.catalogue_entry("tokenizers")
    assert entry["kind"] == "package" and entry["restart"] is False
    present = {"onnxruntime"}
    monkeypatch.setattr(components.importlib.util, "find_spec",
                        lambda name: object() if name in present else None)
    assert not entry["present"]()
    present.add("tokenizers")
    assert entry["present"]()

