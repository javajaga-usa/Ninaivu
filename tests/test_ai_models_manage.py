"""The AI models page: one click for every model the household needs, and a
Delete for each one that leaves the rest of Ninaivu as it was.

Asked for on 2026-11-21: "One click all models download needed. Each model
can be deleted or able to be updated. If deleted should not affect core
functionalities." The real models are hundreds of megabytes, so these use
small stand-in catalogue entries; no test writes a file of a real model's
size.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ninaivu.api import ai_models_api
from ninaivu.media import model_catalog
from ninaivu.storage import db, resume


def _toy(folder: str, *names: tuple[str, int], settings: dict | None = None) -> dict:
    entry = {"label": f"Toy {folder}", "used_for": "Testing", "licence": "MIT",
             "source": "here", "runs_on": "CPU", "requires": [],
             "files": [{"url": f"https://example.invalid/{n}", "name": n, "folder": folder,
                        "bytes": size, "sha256": "0" * 64} for n, size in names]}
    if settings:
        entry["settings"] = settings
    return entry


def _place(model_id: str) -> None:
    """Put a toy model's files on disk at their pinned sizes."""
    for entry in model_catalog.MODELS[model_id]["files"]:
        path = model_catalog.file_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * entry["bytes"])


@pytest.fixture()
def toys(monkeypatch, tmp_path):
    model_catalog.configure(tmp_path / "models")
    shared = ("vad.onnx", 7)
    monkeypatch.setitem(model_catalog.MODELS, "toy-a", _toy("toy-a", ("a.onnx", 10)))
    monkeypatch.setitem(model_catalog.MODELS, "toy-b",
                        _toy("speechy", ("big.onnx", 12), settings={"toy_model": "speechy/big.onnx"}))
    monkeypatch.setitem(model_catalog.MODELS, "toy-c", _toy("speechy", ("small.onnx", 5)))
    # One file both speech-like toys use, as both Whisper sizes share the
    # speech detector.
    model_catalog.MODELS["toy-b"]["files"].append(
        {"url": "https://example.invalid/vad", "name": shared[0], "folder": "speechy",
         "bytes": shared[1], "sha256": "0" * 64})
    model_catalog.MODELS["toy-c"]["files"].append(dict(model_catalog.MODELS["toy-b"]["files"][-1]))
    yield tmp_path / "models"
    for model_id in ("toy-a", "toy-b", "toy-c"):
        model_catalog.downloads.cancel(model_id)
    model_catalog.configure(None)


# ---------------------------------------------------------------------------
# Removing a model
# ---------------------------------------------------------------------------

def test_delete_removes_the_files_and_the_empty_folder_and_nothing_else(toys):
    _place("toy-a")
    (toys / "keep-me.txt").write_text("somebody's")
    leftover = toys / "toy-a" / ".a.onnx.abc.part"
    leftover.write_bytes(b"half")
    assert model_catalog.installed("toy-a")
    assert model_catalog.describe("toy-a")["on_disk_bytes"] == 10

    outcome = model_catalog.remove("toy-a")

    assert outcome["removed"] == ["a.onnx"] and outcome["freed_bytes"] == 10
    assert not model_catalog.installed("toy-a")
    assert not (toys / "toy-a").exists(), "its folder, partial download and all"
    assert (toys / "keep-me.txt").exists() and toys.is_dir(), "the models folder stays"


def test_a_file_another_installed_model_uses_is_kept(toys):
    _place("toy-b")
    _place("toy-c")
    outcome = model_catalog.remove("toy-b")
    assert outcome["kept_shared"] == ["vad.onnx"]
    assert not model_catalog.installed("toy-b")
    assert model_catalog.installed("toy-c"), "the other one still works"
    # With nobody else using it, the shared file goes with the last one.
    model_catalog.remove("toy-c")
    assert not (toys / "speechy").exists()


def test_delete_takes_the_models_setting_out(toys):
    _place("toy-b")
    model_catalog.apply_settings("toy-b")
    settings = model_catalog.settings_path()
    current = json.loads(settings.read_text())
    current["other_model"] = "/somewhere/else"
    model_catalog.write_settings(settings, current)
    model_catalog.remove("toy-b")
    left = json.loads(settings.read_text())
    assert "toy_model" not in left, "the tool says not installed, not 'file missing'"
    assert left["other_model"] == "/somewhere/else"


def test_a_model_that_is_downloading_is_not_deleted(toys):
    _place("toy-a")
    with model_catalog.downloads._lock:
        model_catalog.downloads._state["toy-a"] = {"status": "downloading"}
    with pytest.raises(model_catalog.ModelBusy):
        model_catalog.remove("toy-a")
    assert model_catalog.installed("toy-a")
    with model_catalog.downloads._lock:
        model_catalog.downloads._state.pop("toy-a")


# ---------------------------------------------------------------------------
# One after another
# ---------------------------------------------------------------------------

def test_the_queue_downloads_one_model_at_a_time(toys):
    order, overlap = [], []
    running = threading.Event()
    downloads = model_catalog.Downloads()

    def start(model_id):
        if running.is_set():
            overlap.append(model_id)
        running.set()
        with downloads._lock:
            downloads._state[model_id] = {"status": "downloading"}

        def finish():
            time.sleep(0.05)
            _place(model_id)
            order.append(model_id)
            running.clear()
            with downloads._lock:
                downloads._state[model_id]["status"] = "installed"
                downloads._ended.notify_all()
        threading.Thread(target=finish, daemon=True).start()

    _place("toy-c")
    added = downloads.enqueue(["toy-a", "toy-b", "toy-c", "not-a-model"], start)
    assert added == ["toy-a", "toy-b"], "installed and unknown ones stepped over"
    assert downloads.state("toy-b")["status"] in ("queued", "downloading")
    for _ in range(200):
        if len(order) == 2 and downloads._queue_worker is None:
            break
        time.sleep(0.02)
    assert order == ["toy-a", "toy-b"] and not overlap


def test_a_waiting_model_can_be_taken_out_of_the_queue(toys):
    downloads = model_catalog.Downloads()
    gate = threading.Event()
    started = []

    def start(model_id):
        started.append(model_id)
        with downloads._lock:
            downloads._state[model_id] = {"status": "downloading"}
        if model_id == "toy-a":
            gate.wait(5)
        with downloads._lock:
            downloads._state[model_id]["status"] = "installed"
            downloads._ended.notify_all()

    downloads.enqueue(["toy-a", "toy-b"], start)
    for _ in range(100):
        if started:
            break
        time.sleep(0.01)
    assert downloads.cancel("toy-b")
    assert not downloads.cancel("toy-a"), "the one downloading is left to finish"
    gate.set()
    for _ in range(100):
        if downloads._queue_worker is None:
            break
        time.sleep(0.02)
    assert started == ["toy-a"]


# ---------------------------------------------------------------------------
# What "needed" means
# ---------------------------------------------------------------------------

def test_needed_is_one_model_per_job_and_never_the_experimental_one(cfg, monkeypatch):
    from ninaivu.runtime import florence
    monkeypatch.setattr(model_catalog, "missing_packages", lambda m: [])
    monkeypatch.setattr(model_catalog, "installed", lambda m: m == "orientation")
    monkeypatch.setattr(florence, "suggested_model", lambda memory_bytes=None: "captions-large")
    cfg.ai_enabled = True
    cfg.ai_runtime = True
    cfg.colour_photos = True
    cfg.photo_talk = True
    cfg.letters_aloud = False
    needed = ai_models_api.needed_models(cfg)["ids"]
    assert "faces" in needed and "orientation" not in needed, "installed ones are not asked for"
    assert "search-onnx" in needed and "siglip2" not in needed
    assert "captions-large" in needed and "captions" not in needed
    assert "speech" in needed and "speech-quick" not in needed
    assert {"colour", "text-model", "vision-part"} <= set(needed)
    assert "generative" not in needed and "faces-arcface" not in needed
    assert "voice-tamil" not in needed, "letters aloud is off"


def test_a_model_without_its_packages_is_listed_as_skipped(cfg, monkeypatch):
    monkeypatch.setattr(model_catalog, "installed", lambda m: False)
    monkeypatch.setattr(model_catalog, "missing_packages",
                        lambda m: ["llama_cpp"] if m in ("text-model", "vision-part") else [])
    cfg.ai_runtime = True
    cfg.memory_films = True
    needed = ai_models_api.needed_models(cfg)
    assert "text-model" not in needed["ids"]
    assert {"id": "text-model", "label": model_catalog.MODELS["text-model"]["label"],
            "packages": ["llama_cpp"]} in needed["skipped"]
    assert needed["bytes"] == sum(model_catalog.total_bytes(m) for m in needed["ids"])


# ---------------------------------------------------------------------------
# The page's routes
# ---------------------------------------------------------------------------

def test_the_page_offers_the_needed_models(as_admin):
    payload = as_admin.get("/api/admin/ai-models").get_json()
    assert "faces" in payload["needed"]["ids"] and payload["queued"] == []
    assert all("on_disk_bytes" in m for m in payload["models"])


def test_download_needed_queues_them_and_remembers_them(as_admin, scanned, monkeypatch):
    cfg, conn, _ = scanned
    asked = []
    monkeypatch.setattr(ai_models_api, "needed_models",
                        lambda c: {"ids": ["faces", "orientation"], "bytes": 1, "skipped": []})
    monkeypatch.setattr(model_catalog.downloads, "enqueue",
                        lambda ids, start: asked.extend(ids) or list(ids))
    response = as_admin.post("/api/admin/ai-models/download-needed", json={})
    assert response.status_code == 202
    assert asked == ["faces", "orientation"]
    wanted = resume.wanted(conn)
    assert wanted["model:faces"]["queued"] is True, "a restart carries the whole list on"
    assert conn.execute("SELECT 1 FROM audit WHERE action='ai_model_download'").fetchone()


def test_start_up_carries_the_queue_on_one_after_another(cfg, monkeypatch):
    from ninaivu import Services
    services = Services(cfg)
    conn = db.connect(cfg.db_path)
    resume.want(conn, "model:faces", {"model_id": "faces", "queued": True})
    resume.want(conn, "model:orientation", {"model_id": "orientation", "queued": True})
    resume.want(conn, "model:lama", {"model_id": "lama"})
    calls = []
    monkeypatch.setattr(model_catalog, "installed", lambda m: False)
    monkeypatch.setattr(ai_models_api, "queue_model_downloads",
                        lambda path, ids: calls.append(("queue", list(ids))))
    monkeypatch.setattr(ai_models_api, "start_model_download",
                        lambda path, m, force=False: calls.append(("start", m)))
    services._resume_jobs(jobs=False)
    assert ("queue", ["faces", "orientation"]) in calls
    assert ("start", "lama") in calls


def test_delete_route(as_admin, scanned, toys, monkeypatch):
    cfg, conn, _ = scanned
    _place("toy-a")
    assert as_admin.delete("/api/admin/ai-models/nothing-of-that-name").status_code == 404
    response = as_admin.delete("/api/admin/ai-models/toy-a")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["removed"] == ["a.onnx"]
    assert not next(m for m in body["models"] if m["id"] == "toy-a")["installed"]
    assert conn.execute("SELECT detail FROM audit WHERE action='ai_model_delete'").fetchone()


def test_delete_route_waits_for_a_download(as_admin, toys):
    _place("toy-a")
    with model_catalog.downloads._lock:
        model_catalog.downloads._state["toy-a"] = {"status": "downloading"}
    try:
        assert as_admin.delete("/api/admin/ai-models/toy-a").status_code == 409
        assert model_catalog.installed("toy-a")
    finally:
        with model_catalog.downloads._lock:
            model_catalog.downloads._state.pop("toy-a")


def test_a_file_in_use_is_reported_and_the_rest_removed(as_admin, toys, monkeypatch):
    _place("toy-b")
    real = Path.unlink

    def stubborn(self, *args, **kwargs):
        if self.name == "big.onnx":
            raise PermissionError("in use")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stubborn)
    response = as_admin.delete("/api/admin/ai-models/toy-b")
    assert response.status_code == 409
    body = response.get_json()
    assert "big.onnx" in body["error"] and body["said"], "a sentence the page translates"
    assert not (toys / "speechy" / "vad.onnx").exists()


def test_delete_lets_go_of_the_model_hosts(as_admin, app, toys):
    _place("toy-a")
    services = app.config["MV_SERVICES"]
    closed = []
    services.close_model_host = lambda: closed.append(True)
    assert as_admin.delete("/api/admin/ai-models/toy-a").status_code == 200
    assert closed, "so a model in use lets go of its files"


# ---------------------------------------------------------------------------
# Deleted, and the rest of Ninaivu still works
# ---------------------------------------------------------------------------

class _SearchGone:
    """A picture search whose model was deleted under it."""

    semantic = True
    model_id = "ViT-B-16-SigLIP2/webli-squash"
    name = "onnx"

    def encode_text(self, text):
        raise RuntimeError("the search model is not downloaded")


def test_search_answers_by_words_when_its_model_is_gone(as_family, app):
    services = app.config["MV_SERVICES"]
    services.engine = _SearchGone()
    response = as_family.get("/api/assets?q=shot0")
    assert response.status_code == 200
    assert response.get_json()["items"], "found by name, without the picture search"


def test_deleting_the_search_model_loads_search_again(monkeypatch):
    from ninaivu import ai
    built = []

    class Light:
        name = "light"
        model_id = "heuristic-v1"

    monkeypatch.setattr(ai, "build_engine", lambda cfg, force=False: built.append(force) or Light())
    services = type("S", (), {"engine": None, "scanner": type("Sc", (), {"ai": None})()})()
    ai_models_api.reload_search(services, object())
    assert built == [True]
    assert services.engine is not None and services.scanner.ai is services.engine


def test_search_uses_knows_which_engine_came_from_which_model(monkeypatch):
    from ninaivu import ai
    onnx = ai.OnnxEngine.__new__(ai.OnnxEngine)
    monkeypatch.setattr(ai, "_engine", onnx)
    assert ai_models_api.search_uses("search-onnx")
    assert not ai_models_api.search_uses("siglip2")
    assert not ai_models_api.search_uses("faces")
    monkeypatch.setattr(ai, "_engine", ai.LightEngine())
    assert not ai_models_api.search_uses("search-onnx")


def test_core_pages_work_with_no_model_on_this_computer(as_admin, as_family):
    """Every model deleted: browsing, a photo, the People and Straighten
    pages, the Playground and the models page all answer, each AI feature
    saying it is not installed."""
    assert not any(model_catalog.installed(m) for m in model_catalog.MODELS)
    items = as_family.get("/api/assets?limit=5").get_json()["items"]
    assert items
    assert as_family.get(f"/api/asset/{items[0]['id']}").status_code == 200
    assert as_family.get(f"/api/thumb/{items[0]['id']}").status_code in (200, 304)
    caps = as_admin.get("/api/ai-playground/capabilities")
    assert caps.status_code == 200
    body = caps.get_json()
    assert body.get("segmentation_model") is False
    models = as_admin.get("/api/admin/ai-models").get_json()
    assert models["engine"]["descriptions"]["waits_for"], "says the description model is not here"
    for path in ("/api/faces/status", "/api/straighten/status"):
        response = as_admin.get(path)
        assert response.status_code == 200, (path, response.status_code)
