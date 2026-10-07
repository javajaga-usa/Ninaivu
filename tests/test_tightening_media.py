"""The tightening pass over ninaivu/media: loose ends, each pinned by a test."""

import gc
import io
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ninaivu.media import (ai_editing, audio_art, date_edit, face_parser, faces,
                           jobs, media, orientnet, stills, stripped_video)


# -- face models that arrive while Ninaivu runs are used without a restart ----

def test_a_face_engine_picks_up_models_downloaded_after_it_was_made(tmp_path, monkeypatch):
    if not faces._opencv_support()["available"]:
        pytest.skip("this OpenCV has no face models")
    monkeypatch.setattr(faces, "models_dir", lambda state_dir=None: tmp_path)
    monkeypatch.setattr(faces.cv2, "FaceDetectorYN",
                        SimpleNamespace(create=lambda *a, **k: object()))
    monkeypatch.setattr(faces.cv2, "FaceRecognizerSF",
                        SimpleNamespace(create=lambda *a, **k: object()))
    engine = faces.FaceEngine(tmp_path)
    assert not engine.available
    assert "not downloaded" in engine.unavailable_reason

    for spec in faces.MODELS.values():
        (tmp_path / spec["file"]).write_bytes(b"model")
    assert engine.available


def test_a_closed_face_engine_stays_closed(tmp_path, monkeypatch):
    if not faces._opencv_support()["available"]:
        pytest.skip("this OpenCV has no face models")
    monkeypatch.setattr(faces, "models_dir", lambda state_dir=None: tmp_path)
    engine = faces.FaceEngine(tmp_path)
    engine.close()
    for spec in faces.MODELS.values():
        (tmp_path / spec["file"]).write_bytes(b"model")
    assert not engine.available


# -- cover art is not decoded when it declares an enormous picture ------------

def test_cover_art_declaring_too_many_pixels_is_never_decoded(monkeypatch):
    buffer = io.BytesIO()
    Image.new("1", (9000, 9000)).save(buffer, "PNG")      # 81 MP, a few KB
    loaded = []
    monkeypatch.setattr(Image.Image, "load", lambda self: loaded.append(1))
    assert audio_art.cover_image(buffer.getvalue()) is None
    assert not loaded


def test_ordinary_cover_art_is_still_read():
    buffer = io.BytesIO()
    Image.new("RGB", (600, 600), (10, 20, 30)).save(buffer, "JPEG")
    cover = audio_art.cover_image(buffer.getvalue())
    assert cover is not None and cover.mode == "RGB"


# -- a failed thumbnail leaves no half-written file ----------------------------

def test_a_thumbnail_that_fails_to_save_leaves_nothing_behind(tmp_path, monkeypatch):
    def fails(self, fp, *args, **kwargs):
        Path(fp).write_bytes(b"half")
        raise OSError("No space left on device")

    monkeypatch.setattr(Image.Image, "save", fails)
    with pytest.raises(OSError):
        media.write_thumbnails(Image.new("RGB", (800, 600)), tmp_path, "ab/abc", (640, 256))
    assert not list(tmp_path.rglob("*.tmp"))


# -- opening an original checks there is the memory for it --------------------

def test_an_original_too_large_for_the_memory_free_is_refused(tmp_path, monkeypatch):
    psutil = pytest.importorskip("psutil")
    path = tmp_path / "huge.png"
    Image.new("1", (12000, 12000)).save(path)              # 288 MB to decode, tiny file
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=1024 * 1024))
    with pytest.raises(OSError, match="memory"):
        media._open_oriented(path)


# -- a video ffmpeg gave up on is not handed to OpenCV ------------------------

def test_a_video_ffmpeg_timed_out_on_is_not_read_by_opencv(tmp_path, monkeypatch):
    def times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 45)

    opened = []
    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(media, "FFPROBE", "ffprobe")
    monkeypatch.setattr(media.subprocess, "run", times_out)
    if media.cv2 is not None:
        monkeypatch.setattr(media.cv2, "VideoCapture", lambda *a: opened.append(a))
    assert media.extract_video_frame(tmp_path / "stuck.mp4") is None
    assert media.probe_video(tmp_path / "stuck.mp4") == {}
    assert not opened


# -- a date change on a drive without hard links renames, never copies --------

def test_a_move_without_hard_links_renames_on_the_same_drive(tmp_path, monkeypatch):
    source = tmp_path / "a.jpg"
    source.write_bytes(b"photo")
    (tmp_path / "2024").mkdir()
    target = tmp_path / "2024" / "a.jpg"

    def no_links(*args):
        raise PermissionError("no hard links on exFAT")

    copied = []
    monkeypatch.setattr(date_edit.os, "link", no_links)
    monkeypatch.setattr(date_edit, "copy_exclusive", lambda *a: copied.append(a))
    date_edit._move(source, target)
    assert target.read_bytes() == b"photo"
    assert not source.exists()
    assert not copied


def test_a_move_without_hard_links_never_replaces_a_file(tmp_path, monkeypatch):
    source = tmp_path / "a.jpg"
    source.write_bytes(b"new")
    target = tmp_path / "b.jpg"
    target.write_bytes(b"old")
    monkeypatch.setattr(date_edit.os, "link",
                        lambda *a: (_ for _ in ()).throw(PermissionError("no links")))
    with pytest.raises(FileExistsError):
        date_edit._move(source, target)
    assert target.read_bytes() == b"old" and source.exists()


# -- asking whether the orientation model is there does not load it -----------

def test_asking_for_the_orientation_model_does_not_load_it(tmp_path, monkeypatch):
    if not orientnet._opencv_support()["available"]:
        pytest.skip("this OpenCV has no dnn module")
    (tmp_path / orientnet.MODEL["file"]).write_bytes(b"not really onnx")
    loads = []

    def refuse(path):
        loads.append(path)
        raise ValueError("not a model")

    monkeypatch.setattr(orientnet.cv2.dnn, "readNetFromONNX", refuse)
    monkeypatch.setattr(orientnet, "_unloadable", set())
    orientnet.reset()
    assert orientnet.available(tmp_path) is True
    assert not loads
    # Once OpenCV has refused the file, it is not offered, nor tried again.
    assert orientnet._net(tmp_path) is None
    assert orientnet._net(tmp_path) is None
    assert len(loads) == 1
    assert orientnet.available(tmp_path) is False
    orientnet.reset()


# -- a face-parsing model placed while Ninaivu runs is found ------------------

def test_a_face_parsing_model_placed_later_is_found(tmp_path, monkeypatch):
    if face_parser.cv2 is None:
        pytest.skip("needs OpenCV")
    path = tmp_path / "faceparse" / "face_parsing.onnx"
    monkeypatch.setattr(face_parser, "model_path", lambda: path)
    monkeypatch.setattr(face_parser, "FaceParser", lambda p: ("parser", p))
    face_parser.forget()
    assert face_parser.get() is None
    path.parent.mkdir()
    path.write_bytes(b"onnx")
    assert face_parser.get() == ("parser", path)
    face_parser.forget()


# -- a slow local model is called slow ----------------------------------------

def test_a_local_model_that_times_out_is_not_called_unreachable(monkeypatch):
    class Slow:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            raise TimeoutError("timed out")

        def close(self):
            pass

    monkeypatch.setattr(ai_editing, "model_name", lambda: "llama3")
    monkeypatch.setattr(ai_editing.http.client, "HTTPConnection", Slow)
    with pytest.raises(RuntimeError) as error:
        ai_editing.plan("brighten it", {})
    assert "90 seconds" in str(error.value)
    assert "Start Ollama" not in str(error.value)


# -- the stills cache removes its version notes with the pictures -------------

def test_clearing_the_stills_cache_removes_the_version_notes(tmp_path):
    store = stills.StillStore(tmp_path)
    folder = stills.renditions_dir(tmp_path)
    folder.mkdir()
    for name in ("1-v1.jpg", "2-v1-t90.jpg"):
        (folder / name).write_bytes(b"x")
        (folder / f"{name}.source").write_text("v")
    assert store.clear() == 2
    assert not list(folder.iterdir())


def test_evicting_a_still_removes_its_version_note(tmp_path):
    store = stills.StillStore(tmp_path, cache_mb=0)
    folder = stills.renditions_dir(tmp_path)
    folder.mkdir()
    (folder / "1-v1.jpg").write_bytes(b"x" * 100)
    (folder / "1-v1.jpg.source").write_text("v")
    assert store.evict() == 1
    assert not list(folder.iterdir())


def test_a_still_removed_mid_look_is_not_an_error(tmp_path, monkeypatch):
    store = stills.StillStore(tmp_path)
    target = store.path_for(7)
    real_stat = Path.stat

    def gone(self, *args, **kwargs):
        if self == target:
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", gone)
    assert store.ready(7) is None


# -- uncollected results do not wait for the next job to be freed -------------

def test_an_uncollected_result_is_dropped_without_another_job_starting(monkeypatch):
    jobs.reset()
    done = threading.Event()

    def work(report):
        done.set()
        return b"png"

    job_id = jobs.start(1, "upscale", work)
    done.wait(5)
    deadline = time.time() + 5
    while jobs.status(job_id, 1)["state"] != "done" and time.time() < deadline:
        time.sleep(0.01)
    monkeypatch.setattr(jobs, "KEEP_FOR", 0)
    time.sleep(0.01)
    assert jobs.status("someone-else", 1) is None
    assert not jobs._jobs
    jobs.reset()


# -- per-photograph locks are not kept for ever -------------------------------

@pytest.mark.parametrize("module", [stills, stripped_video])
def test_per_item_locks_are_let_go(module):
    lock = module._lock_for(1)
    assert module._lock_for(1) is lock
    for asset_id in range(100, 1100):
        module._lock_for(asset_id)
    gc.collect()
    assert len(module._locks) <= 1


def test_a_stripped_copy_removed_mid_look_is_made_again(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(media, "FFMPEG", "ffmpeg")
    target = stripped_video._target(tmp_path, 5, source)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"copy")
    real_stat = Path.stat

    def gone(self, *args, **kwargs):
        if self == target:
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    def remux(command, **kwargs):
        Path(command[-1]).write_bytes(b"fresh")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(Path, "stat", gone)
    monkeypatch.setattr(stripped_video.subprocess, "run", remux)
    monkeypatch.setattr(stripped_video, "_evict", lambda *a, **k: None)
    assert stripped_video.stripped_copy(tmp_path, 5, source) == target
