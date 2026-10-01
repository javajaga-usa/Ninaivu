"""Local AI models: the catalogue and its downloads, the ONNX tools, SigLIP 2 search.

The tools are tested twice over. Their geometry — which pixels change, how
tiles are stitched, where faces are pasted — is checked with a stand-in model,
so it runs everywhere. The real models run too when they have been downloaded
(they are hundreds of megabytes and not part of the repository).
"""
import base64
import hashlib
import io
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from conftest import ADMIN, FAMILY, login
from ninaivu import ai
from ninaivu.media import model_catalog, onnx_tools
from ninaivu.storage import db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import fetch_ai_models  # noqa: E402


def _png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


# --- the catalogue ---------------------------------------------------------------

def test_every_file_is_pinned_to_exact_bytes():
    """However a file is named, it is pinned to one exact set of bytes.

    Most are a Hugging Face repository at an immutable commit. The three the
    gallery itself needs — the face detector, the recogniser, the orientation
    model — are a plain URL, which cannot be pinned by revision. Either way
    the checksum and the size are what a download is checked against, and
    those are what this is really about.
    """
    for model_id, model in model_catalog.MODELS.items():
        assert model["licence"] and model["source"] and model["files"], model_id
        for entry in model["files"]:
            where = (model_id, entry["name"])
            if "url" in entry:
                assert entry["url"].startswith("https://"), where
            else:
                assert re.fullmatch(r"[0-9a-f]{40}", entry["revision"]), where
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), where
            assert entry["bytes"] > 0


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _catalog_in(tmp_path, monkeypatch, content=b"model bytes"):
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "models"))
    entry = {"repo": "example/model", "revision": "a" * 40, "name": "tiny.onnx", "folder": "onnx",
             "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    monkeypatch.setitem(model_catalog.MODELS, "tiny", {
        "label": "Tiny", "used_for": "tests", "licence": "MIT", "source": "here", "runs_on": "CPU",
        "requires": [], "files": [entry]})
    return entry


def test_a_download_is_published_only_when_its_checksum_matches(tmp_path, monkeypatch):
    entry = _catalog_in(tmp_path, monkeypatch)
    requested = []

    def opener(request, timeout):
        requested.append(request.full_url)
        return _Response(b"model bytes")

    seen = []
    path = model_catalog.download_file(entry, seen.append, opener)
    assert path.read_bytes() == b"model bytes" and sum(seen) == len(b"model bytes")
    assert requested == [f"https://huggingface.co/example/model/resolve/{'a' * 40}/tiny.onnx"]
    assert model_catalog.installed("tiny")

    path.unlink()
    with pytest.raises(model_catalog.ChecksumMismatch):
        model_catalog.download_file(entry, None, lambda request, timeout: _Response(b"tampered!!!"))
    assert not path.exists()
    assert not list(path.parent.glob("*.part")), "a rejected download was left behind"


def test_background_downloads_report_progress_and_failure(tmp_path, monkeypatch):
    _catalog_in(tmp_path, monkeypatch)
    downloads = model_catalog.Downloads()
    assert downloads.start("tiny", opener=lambda request, timeout: _Response(b"model bytes"))
    for _ in range(100):
        if downloads.state("tiny").get("status") != "downloading":
            break
        __import__("time").sleep(0.02)
    assert downloads.state("tiny")["status"] == "installed"
    assert not downloads.start("tiny"), "an installed model was downloaded again"

    model_catalog.file_path(model_catalog.MODELS["tiny"]["files"][0]).unlink()
    failing = model_catalog.Downloads()
    failing.start("tiny", opener=lambda request, timeout: _Response(b"wrong bytes"))
    for _ in range(100):
        if failing.state("tiny").get("status") != "downloading":
            break
        __import__("time").sleep(0.02)
    assert failing.state("tiny")["status"] == "failed" and "checksum" in failing.state("tiny")["error"]


def test_the_command_line_lists_and_refuses_unknown_models(capsys):
    assert fetch_ai_models.main([]) == 0
    assert "lama" in capsys.readouterr().out
    assert fetch_ai_models.main(["--get", "no-such-model"]) == 2


# --- the tools, with a stand-in model -----------------------------------------------

def _stand_in(monkeypatch, behaviour):
    monkeypatch.setattr(onnx_tools, "_run", behaviour)


def test_object_removal_changes_only_the_painted_area(monkeypatch):
    def lama(model_id, feeds):
        assert model_id == "lama" and feeds["image"].shape == (1, 3, 512, 512)
        filled = feeds["image"] * 255
        filled[:, 0][feeds["mask"][:, 0] > 0] = 255        # paint the hole red
        return filled

    _stand_in(monkeypatch, lama)
    photo = Image.new("RGB", (900, 600), (20, 120, 60))
    mask = Image.new("L", photo.size, 0)
    ImageDraw.Draw(mask).rectangle((400, 250, 460, 300), fill=255)
    result = np.asarray(Image.open(io.BytesIO(onnx_tools.remove(_png(photo), _png(mask)))))
    assert result.shape == (600, 900, 3)
    assert result[275, 430, 0] > 200, "the painted area was not filled"
    untouched = np.ones((600, 900), bool)
    untouched[230:320, 380:480] = False
    assert (result[untouched] == (20, 120, 60)).all(), "pixels outside the painted area changed"
    with pytest.raises(ValueError, match="Paint"):
        onnx_tools.remove(_png(photo), _png(Image.new("L", photo.size, 0)))


def test_upscaling_stitches_tiles_back_into_one_image(monkeypatch):
    def nearest(model_id, feeds):
        tile = feeds["input"]
        return tile.repeat(4, axis=2).repeat(4, axis=3)

    _stand_in(monkeypatch, nearest)
    rng = np.random.default_rng(4)
    photo = Image.fromarray(rng.integers(0, 256, (300, 610, 3), dtype=np.uint8))
    result = np.asarray(Image.open(io.BytesIO(onnx_tools.upscale(_png(photo)))))
    expected = np.asarray(photo.resize((2440, 1200), Image.Resampling.NEAREST))
    assert result.shape == expected.shape
    assert np.abs(result.astype(int) - expected.astype(int)).max() <= 1, "tiles were misplaced"
    with pytest.raises(ValueError, match="2048"):
        onnx_tools.upscale(_png(Image.new("RGB", (2100, 10))))


def test_face_restoration_pastes_back_only_around_faces(monkeypatch):
    _stand_in(monkeypatch, lambda model_id, feeds: np.ones_like(feeds["input"]))   # white
    photo = Image.new("RGB", (800, 600), (30, 30, 30))
    monkeypatch.setattr(onnx_tools, "_faces", lambda image: [(100, 100, 100, 100)])
    result = np.asarray(Image.open(io.BytesIO(onnx_tools.restore(_png(photo)))))
    assert result[150, 150].min() > 200, "the face was not restored"
    assert (result[500:, 500:] == 30).all(), "pixels far from the face changed"
    monkeypatch.setattr(onnx_tools, "_faces", lambda image: [])
    with pytest.raises(ValueError, match="No faces"):
        onnx_tools.restore(_png(photo))


def test_a_missing_model_says_where_to_get_it(monkeypatch):
    monkeypatch.setattr(onnx_tools, "available", lambda model_id: False)
    with pytest.raises(RuntimeError, match="AI models tab"):
        onnx_tools._run("upscale", {})
    with pytest.raises(RuntimeError, match="ddcolor.onnx"):
        onnx_tools._run("colorize", {})


def test_colourising_keeps_the_light_and_lays_colour_under_it(monkeypatch):
    """The model sees lightness alone as a grey square, and its colour comes
    back under the photograph's own lightness at full size."""
    seen = {}

    def ddcolor(model_id, feeds):
        seen["model"] = model_id
        grey = feeds["input"]
        assert grey.shape == (1, 3, 512, 512) and grey.dtype == np.float32
        assert 0 <= grey.min() and grey.max() <= 1
        assert np.abs(grey[0, 0] - grey[0, 2]).max() < 1e-6, "the model was shown colour, not lightness"
        # Warm on the left half, cool on the right: a and b planes.
        a = np.where(np.arange(512)[None, :] < 256, 30.0, -20.0).astype(np.float32)
        b = np.where(np.arange(512)[None, :] < 256, 30.0, -30.0).astype(np.float32)
        return np.stack([np.broadcast_to(a, (512, 512)), np.broadcast_to(b, (512, 512))])[None]

    _stand_in(monkeypatch, ddcolor)
    photo = Image.new("RGB", (640, 400), (120, 120, 120))
    reports = []
    result = np.asarray(Image.open(io.BytesIO(onnx_tools.colorize(_png(photo), reports.append))))
    assert seen["model"] == "colorize" and result.shape == (400, 640, 3)
    assert reports[-1] == {"stage": "running", "value": 3, "max": 3}
    left, right = result[200, 100].astype(int), result[200, 540].astype(int)
    assert left[0] > left[2] + 30, "the left was not warmed"
    assert right[2] > right[0] + 20, "the right was not cooled"
    # The light is the photograph's own: a mid grey stays a mid tone either side.
    before = onnx_tools.lightness(np.full((1, 1, 3), 120 / 255.0))[0, 0]
    for pixel in (left, right):
        assert abs(onnx_tools.lightness(pixel[None, None] / 255.0)[0, 0] - before) < 2
    with pytest.raises(ValueError, match="4096"):
        onnx_tools.colorize(_png(Image.new("RGB", (4100, 10))))


def test_the_colourising_model_is_found_by_hand_or_by_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "_root", None)
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.delenv("NINAIVU_COLORIZE_MODEL", raising=False)
    monkeypatch.setattr(onnx_tools, "local_setting", lambda name, default="": default)
    assert onnx_tools.colorize_model_path() is None and not onnx_tools.available("colorize")
    placed = tmp_path / "models" / "onnx" / "ddcolor.onnx"
    placed.parent.mkdir(parents=True)
    placed.write_bytes(b"weights")
    assert onnx_tools.colorize_model_path() == placed
    monkeypatch.setattr(onnx_tools, "_onnxruntime_present", lambda: True)
    assert onnx_tools.available("colorize")
    assert onnx_tools._model_path("colorize") == str(placed)
    monkeypatch.setenv("NINAIVU_COLORIZE_MODEL", str(tmp_path / "elsewhere.onnx"))
    assert onnx_tools.colorize_model_path() == tmp_path / "elsewhere.onnx"
    assert not onnx_tools.available("colorize"), "a path that is not there counted as installed"
    assert onnx_tools.label("colorize") == "Colourise (DDColor)" and "Real-ESRGAN" in onnx_tools.label("upscale")


# --- the tools, with the real models when present ----------------------------------

#: The one thing in the suite that wants the models that are really on this
#: machine. Everything else is pointed at an empty folder per test — see
#: `_models_stay_off_this_machine` in conftest — and `real_models` is how a
#: test says it means the real ones. Without that opt-out the skip below is
#: decided at collection, when they are there, and the test then runs against
#: a folder the fixture has since emptied.
def needs_models(test):
    # Both marks, applied. Written as real_models(skipif(...)) the skip was only
    # an argument to the other mark, never a mark of its own: the test ran on
    # every computer, and failed on any without the models.
    return pytest.mark.real_models(pytest.mark.skipif(
        not all(onnx_tools.available(m) for m in ("lama", "upscale", "restore")),
        reason="the local AI models have not been downloaded")(test))


@needs_models
def test_the_real_models_run_end_to_end():
    photo = Image.open(Path(__file__).parent / "data" / "portrait.jpg").convert("RGB")
    photo.thumbnail((400, 400))
    mask = Image.new("L", photo.size, 0)
    ImageDraw.Draw(mask).rectangle((20, 20, 60, 60), fill=255)
    assert Image.open(io.BytesIO(onnx_tools.remove(_png(photo), _png(mask)))).size == photo.size
    small = photo.resize((64, 80))
    assert Image.open(io.BytesIO(onnx_tools.upscale(_png(small)))).size == (256, 320)
    assert Image.open(io.BytesIO(onnx_tools.restore(_png(photo)))).size == photo.size


# --- the Playground and the console ---------------------------------------------------

def test_local_tools_run_as_background_jobs_when_no_server_takes_them(app, people, monkeypatch):
    from ninaivu.media import jobs
    jobs.reset()
    monkeypatch.setattr(onnx_tools, "available", lambda model_id: model_id in ("upscale", "lama"))
    monkeypatch.setattr(onnx_tools, "upscale", lambda data, report=None: _png(Image.new("RGB", (40, 40))))
    monkeypatch.setattr(onnx_tools, "remove", lambda image, mask: _png(Image.new("RGB", (8, 8), (9, 9, 9))))
    family = login(app.test_client(), *FAMILY)
    caps = family.get("/api/ai-playground/capabilities").get_json()
    assert caps["local_jobs"] == ["upscale"] and caps["object_removal_provider"] == "local-ai"
    # Every Enhance tool is described, set up or not, so the Playground can
    # show the card either way and say what the missing ones need.
    assert caps["enhance_tools"]["upscale"] == {"provider": "local", "ready": True}
    assert caps["enhance_tools"]["restore"]["ready"] is False
    assert "GFPGAN" in caps["enhance_tools"]["restore"]["model"] and not caps["enhance_tools"]["restore"]["by_hand"]
    assert caps["enhance_tools"]["colorize"]["by_hand"] and "DDColor" in caps["enhance_tools"]["colorize"]["model"]

    image = base64.b64encode(_png(Image.new("RGB", (10, 10)))).decode()
    started = family.post("/api/ai-playground/server-jobs", json={"kind": "upscale", "image": image})
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["id"]
    for _ in range(100):
        if family.get(f"/api/ai-playground/server-jobs/{job_id}").get_json()["state"] == "done":
            break
        __import__("time").sleep(0.05)
    result = family.get(f"/api/ai-playground/server-jobs/{job_id}/result")
    assert Image.open(io.BytesIO(result.data)).size == (40, 40)
    assert family.post("/api/ai-playground/server-jobs",
                       json={"kind": "restore", "image": image}).status_code == 404

    removed = family.post("/api/ai-playground/inpaint", json={"image": image, "mask": image})
    assert Image.open(io.BytesIO(removed.data)).getpixel((0, 0)) == (9, 9, 9), "LaMa was not used"


def test_colourising_runs_as_a_local_job_once_its_model_is_placed(app, people, monkeypatch):
    from ninaivu.media import jobs
    jobs.reset()
    monkeypatch.setattr(onnx_tools, "available", lambda model_id: model_id == "colorize")
    monkeypatch.setattr(onnx_tools, "colorize", lambda data, report=None: _png(Image.new("RGB", (10, 10), (200, 120, 60))))
    family = login(app.test_client(), *FAMILY)
    caps = family.get("/api/ai-playground/capabilities").get_json()
    assert caps["local_jobs"] == ["colorize"] and caps["enhance_tools"]["colorize"] == {"provider": "local", "ready": True}
    image = base64.b64encode(_png(Image.new("L", (10, 10), 128).convert("RGB"))).decode()
    started = family.post("/api/ai-playground/server-jobs", json={"kind": "colorize", "image": image})
    assert started.status_code == 202, started.get_json()
    job_id = started.get_json()["id"]
    for _ in range(100):
        if family.get(f"/api/ai-playground/server-jobs/{job_id}").get_json()["state"] == "done":
            break
        __import__("time").sleep(0.05)
    result = family.get(f"/api/ai-playground/server-jobs/{job_id}/result")
    assert Image.open(io.BytesIO(result.data)).getpixel((0, 0)) == (200, 120, 60)


def test_the_console_lists_and_downloads_models(app, people, monkeypatch):
    started = []
    monkeypatch.setattr(model_catalog.downloads, "start",
                        lambda model_id, **kwargs: started.append(model_id) or True)
    admin = login(app.test_client(), *ADMIN)
    listing = admin.get("/api/admin/ai-models").get_json()
    assert {m["id"] for m in listing["models"]} == set(model_catalog.MODELS)
    assert listing["restart_for_search"] is False, "a restart was suggested with AI search turned off"
    assert admin.post("/api/admin/ai-models/lama/download").status_code == 202 and started == ["lama"]
    assert admin.post("/api/admin/ai-models/nope/download").status_code == 404
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/admin/ai-models").status_code in (401, 403, 404)


def test_the_console_endpoints_are_not_on_the_family_port(scanned):
    from ninaivu import build_services, create_home_app
    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    routes = {str(rule) for rule in create_home_app(services).url_map.iter_rules()}
    assert not [r for r in routes if r.startswith("/api/admin/ai-models")]


# --- SigLIP 2 search ---------------------------------------------------------------

def test_auto_uses_siglip_only_once_it_is_downloaded(monkeypatch):
    cfg = SimpleNamespace(clip_model="auto", clip_pretrained="")
    monkeypatch.setattr(model_catalog, "installed", lambda model_id: False)
    assert ai.clip_choice(cfg)[:2] == ("ViT-B-32", "laion2b_s34b_b79k")
    monkeypatch.setattr(model_catalog, "installed", lambda model_id: True)
    monkeypatch.setattr(model_catalog, "missing_packages", lambda model_id: [])
    name, pretrained, _, tokenizer_dir, label = ai.clip_choice(cfg)
    assert name == "ViT-B-16-SigLIP2" and pretrained.endswith("open_clip_model.safetensors")
    assert tokenizer_dir and label == "webli"
    explicit = SimpleNamespace(clip_model="ViT-L-14", clip_pretrained="openai")
    assert ai.clip_choice(explicit)[:2] == ("ViT-L-14", "openai")


def test_changing_the_search_model_reindexes_once(scanned):
    from ninaivu.media.scanner import Scanner
    cfg, conn, _ = scanned
    asset_id = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    db.store_embedding(conn, asset_id, "ViT-B-32/laion2b_s34b_b79k", 4, np.ones(4, "float32").tobytes())
    conn.execute("UPDATE assets SET ai_version=2")
    conn.commit()

    scanner = Scanner(cfg)
    scanner.ai = SimpleNamespace(model_id="ViT-B-16-SigLIP2/webli", semantic=True)
    scanner._retag_if_the_model_changed(conn)
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert conn.execute("SELECT MAX(ai_version) FROM assets").fetchone()[0] == 0
    assert db.get_meta(conn, "ai_model_id") == "ViT-B-16-SigLIP2/webli"

    conn.execute("UPDATE assets SET ai_version=2")
    conn.commit()
    scanner._retag_if_the_model_changed(conn)
    assert conn.execute("SELECT MAX(ai_version) FROM assets").fetchone()[0] == 2, "re-indexed twice"


def test_an_existing_install_on_the_same_model_is_not_reindexed(scanned):
    from ninaivu.media.scanner import Scanner
    cfg, conn, _ = scanned
    asset_id = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    db.store_embedding(conn, asset_id, "ViT-B-32/laion2b_s34b_b79k", 4, np.ones(4, "float32").tobytes())
    conn.execute("UPDATE assets SET ai_version=2")
    conn.commit()
    scanner = Scanner(cfg)
    scanner.ai = SimpleNamespace(model_id="ViT-B-32/laion2b_s34b_b79k", semantic=True)
    scanner._retag_if_the_model_changed(conn)
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1
    assert conn.execute("SELECT MAX(ai_version) FROM assets").fetchone()[0] == 2


def test_the_engine_can_set_its_own_explicit_content_threshold(scanned):
    from ninaivu.media.scanner import Scanner
    cfg, _, _ = scanned
    scanner = Scanner(cfg)
    scanner.ai = SimpleNamespace(nsfw_threshold=0.01)
    assert scanner._nsfw_threshold() == 0.01
    scanner.ai = SimpleNamespace(nsfw_threshold=None)
    assert scanner._nsfw_threshold() == cfg.nsfw_threshold


def needs_siglip(test):
    # The real models folder, as the skip condition was decided against: each
    # test is otherwise given an empty one, so with SigLIP 2 installed this ran
    # against a folder without it and loaded ViT-B-32 instead.
    return pytest.mark.real_models(pytest.mark.skipif(
        not (model_catalog.installed("siglip2") and not model_catalog.missing_packages("siglip2")),
        reason="SigLIP 2 has not been downloaded")(test))


@needs_siglip
def test_siglip_scores_ordinary_photos_as_safe_and_finds_them_by_text():
    engine = ai.ClipEngine(*ai.clip_choice(SimpleNamespace(clip_model="auto", clip_pretrained="")))
    assert engine.sigmoid_scores and engine.nsfw_threshold == 0.01
    data = Path(__file__).parent / "data"
    results = engine.analyse([data / "portrait.jpg", data / "face.jpg"], tag_threshold=engine.tag_threshold)
    for result in results:
        assert result["nsfw_score"] < engine.nsfw_threshold, result
        assert len(result["embedding"]) == 768 * 4
    query = engine.encode_text("an astronaut in a space suit")
    score = float(np.frombuffer(results[0]["embedding"], "float32") @ query)
    assert score >= engine.search_floor, score
