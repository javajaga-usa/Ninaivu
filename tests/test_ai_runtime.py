"""The on-device AI engine (2.0, proposal C): its model host, where models
run, the pinned speech models, and voice stories written down as words.

The speech model itself is a gigabyte and is not downloaded here: what is
tested is everything around it, with a stand-in recogniser that behaves as
sherpa-onnx's does. ``NINAIVU_TEST_WHISPER`` pointing at an unpacked
sherpa-onnx Whisper folder (``tiny`` is enough) runs the real thing too.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import runtime_fakes
from ninaivu.media import model_catalog
from ninaivu.runtime import devices, speech
from ninaivu.runtime.host import (HostCrashed, HostError, HostStopped, HostTimeout,
                                  ModelHost, Unavailable)
from ninaivu.runtime.transcriber import Transcriber
from ninaivu.storage import db, stories, story_words


# ---------------------------------------------------------------------------
# Where a model runs
# ---------------------------------------------------------------------------

def test_a_mac_runs_image_models_on_core_ml_and_falls_back_to_the_processor():
    offered = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert devices.onnx_providers(offered, platform="darwin") == [
        "CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert devices.onnx_providers(offered, platform="linux") == ["CPUExecutionProvider"]


def test_windows_takes_directml_then_cuda_and_only_what_is_installed():
    assert devices.onnx_providers(["DmlExecutionProvider", "CUDAExecutionProvider",
                                   "CPUExecutionProvider"], platform="win32") == [
        "DmlExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
    assert devices.onnx_providers(["CPUExecutionProvider"], platform="win32") == [
        "CPUExecutionProvider"]
    assert devices.onnx_providers([], platform="win32") == ["CPUExecutionProvider"], \
        "the processor is always the last resort, even when nothing was reported"


def test_an_eight_bit_model_stays_on_the_processor():
    assert devices.onnx_providers(["CoreMLExecutionProvider"], quantised=True,
                                  platform="darwin") == ["CPUExecutionProvider"]
    assert devices.sherpa_provider(quantised=True, platform="darwin") == "cpu"
    assert devices.sherpa_provider(quantised=False, platform="darwin") == "coreml"
    assert devices.sherpa_provider(quantised=False, platform="win32") == "cpu"


def test_the_console_is_told_in_words():
    assert devices.label("CoreMLExecutionProvider").startswith("the Neural Engine")
    assert devices.label("cpu") == "the processor"
    assert devices.describe("linux")["speech"] == "the processor"


# ---------------------------------------------------------------------------
# The model host
# ---------------------------------------------------------------------------

@pytest.fixture()
def host(tmp_path):
    made = ModelHost({"models": str(tmp_path), "capabilities": runtime_fakes.ALL,
                      "idle_seconds": 60})
    yield made
    made.close()


def test_the_host_is_a_process_of_its_own_and_is_kept_between_calls(host, tmp_path):
    first = host.call("echo", word="hello")
    assert first["kwargs"] == {"word": "hello"}
    assert first["pid"] != os.getpid(), "the model runs outside the server"
    assert first["models"] == str(tmp_path), "the host is told where the models are"
    second = host.call("echo")
    assert second["pid"] == first["pid"] and second["loaded_at"] == first["loaded_at"], \
        "loaded once, then kept"
    assert host.status()["running"] and host.status()["calls"] == 2


def test_a_crash_costs_one_answer_and_the_next_call_starts_a_new_host(host):
    before = host.call("echo")["pid"]
    with pytest.raises(HostCrashed):
        host.call("crash")
    assert host.status()["crashes"] == 1
    after = host.call("echo")["pid"]
    assert after != before


def test_a_failure_inside_a_capability_is_reported_and_the_host_carries_on(host):
    pid = host.call("echo")["pid"]
    with pytest.raises(HostError, match="this file is not a sound"):
        host.call("boom")
    assert host.call("echo")["pid"] == pid
    assert host.status()["crashes"] == 0


def test_a_capability_that_cannot_run_here_says_so(host):
    with pytest.raises(Unavailable, match="no model for this"):
        host.call("missing")
    with pytest.raises(Unavailable):
        host.call("no-such-thing")


def test_a_call_that_takes_too_long_stops_the_host(host):
    pid = host.call("echo")["pid"]
    started = time.monotonic()
    with pytest.raises(HostTimeout):
        host.call("slow", seconds=30, timeout=1.0)
    assert time.monotonic() - started < 10
    assert host.call("echo")["pid"] != pid


def test_stopping_abandons_the_call_at_once(host):
    host.call("echo")
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    started = time.monotonic()
    with pytest.raises(HostStopped):
        host.call("slow", seconds=30, stop=stop)
    assert time.monotonic() - started < 10


def test_closing_ends_a_call_in_progress_and_every_later_one(host):
    host.call("echo")
    threading.Timer(0.5, host.close).start()
    with pytest.raises(HostStopped):
        host.call("slow", seconds=30)
    with pytest.raises(HostStopped):
        host.call("echo")


def test_an_idle_host_leaves_and_the_next_call_is_not_a_crash(tmp_path):
    host = ModelHost({"models": str(tmp_path), "capabilities": runtime_fakes.ALL,
                      "idle_seconds": 1})
    try:
        first = host.call("echo")["pid"]
        deadline = time.monotonic() + 15
        while host.running and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not host.running, "an idle host gives its memory back"
        assert host.call("echo")["pid"] != first
        assert host.status()["crashes"] == 0
    finally:
        host.close()


# ---------------------------------------------------------------------------
# The pinned speech models
# ---------------------------------------------------------------------------

def _archive(tmp_path, members: dict[str, bytes]) -> bytes:
    path = tmp_path / "bundle.tar.bz2"
    with tarfile.open(path, "w:bz2") as bundle:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return path.read_bytes()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _entry(blob: bytes, take: dict[str, tuple[str, int]]) -> dict:
    return {"url": "https://example.invalid/bundle.tar.bz2", "name": "bundle.tar.bz2",
            "folder": "speechtest", "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "take": {name: {"member": member, "bytes": size}
                     for name, (member, size) in take.items()}}


def test_an_archive_is_checked_then_only_its_named_files_are_taken(tmp_path):
    model_catalog.configure(tmp_path / "models")
    try:
        blob = _archive(tmp_path, {"m/encoder.onnx": b"E" * 100, "m/tokens.txt": b"T" * 7,
                                   "m/test_wavs/0.wav": b"W" * 50, "../escape": b"x"})
        entry = _entry(blob, {"encoder.onnx": ("m/encoder.onnx", 100),
                              "tokens.txt": ("m/tokens.txt", 7)})
        assert not model_catalog.entry_present(entry)
        folder = model_catalog.download_file(entry, opener=lambda *a, **k: _Response(blob))
        assert sorted(p.name for p in folder.iterdir()) == ["encoder.onnx", "tokens.txt"], \
            "the archive is not kept, nor anything it holds that was not named"
        assert (folder / "encoder.onnx").read_bytes() == b"E" * 100
        assert model_catalog.entry_present(entry)
        assert not (tmp_path / "escape").exists()
    finally:
        model_catalog.configure(None)


def test_an_archive_whose_member_is_not_the_pinned_size_installs_nothing(tmp_path):
    model_catalog.configure(tmp_path / "models")
    try:
        blob = _archive(tmp_path, {"m/encoder.onnx": b"E" * 99})
        entry = _entry(blob, {"encoder.onnx": ("m/encoder.onnx", 100)})
        with pytest.raises(model_catalog.ChecksumMismatch):
            model_catalog.download_file(entry, opener=lambda *a, **k: _Response(blob))
        assert not any(p.is_file() for p in (tmp_path / "models").rglob("*"))
        tampered = _entry(blob, {"encoder.onnx": ("m/encoder.onnx", 99)})
        tampered["sha256"] = "0" * 64
        with pytest.raises(model_catalog.ChecksumMismatch):
            model_catalog.download_file(tampered, opener=lambda *a, **k: _Response(blob))
        assert not any(p.is_file() for p in (tmp_path / "models").rglob("*"))
    finally:
        model_catalog.configure(None)


def test_the_speech_models_are_pinned_and_unpack_what_the_reader_loads():
    for model_id in speech.MODELS:
        model = model_catalog.MODELS[model_id]
        assert model["requires"] == ["sherpa_onnx"]
        archive, vad = model["files"]
        assert archive["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
        assert set(archive["take"]) == {"encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"}
        assert vad["name"] == "silero_vad.onnx"
    assert model_catalog.MODELS["speech"]["files"][1] is model_catalog.MODELS["speech-quick"]["files"][1], \
        "one speech detector on disk for both"


def test_large_v3_is_preferred_and_its_words_outrank_turbos(tmp_path, monkeypatch):
    installed = set()
    monkeypatch.setattr(model_catalog, "installed", lambda m: m in installed)
    assert speech.chosen_model() is None and speech.version_of(None) == 0
    installed.add("speech-quick")
    assert speech.chosen_model() == "speech-quick"
    installed.add("speech")
    assert speech.chosen_model() == "speech"
    assert speech.version_of("speech") > speech.version_of("speech-quick") > 0


# ---------------------------------------------------------------------------
# Reading a story
# ---------------------------------------------------------------------------

def test_the_households_languages():
    assert speech.languages("ta, en") == ["ta", "en"]
    assert speech.languages("TA;en;ta;tamil;") == ["ta", "en"]
    assert speech.languages(None) == []


def test_pieces_join_speech_up_to_the_limit_and_stay_inside_the_recording():
    rate = speech.RATE
    spans = [(0, 5 * rate), (6 * rate, 10 * rate), (20 * rate, 30 * rate), (40 * rate, 41 * rate)]
    joined = speech.pieces(spans, 41 * rate)
    assert joined[0][0] == 0, "padding never reaches before the start"
    assert joined[-1][1] == 41 * rate, "nor past the end"
    assert all(end - start <= speech.MAX_PIECE * rate for start, end in joined)
    assert [(a / rate, b / rate) for a, b in joined] == [(0, 10.25), (19.75, 41)], \
        "the first two together; the third would make the piece too long, so a new one"


def test_whisper_loops_are_cut_back():
    assert speech.tidy("இது 4-4-4-4-4-4-4-4-4-4-4- கடை") == "இது 4- கடை"
    assert speech.tidy("our shop our shop our shop our shop our shop our shop in 1968") == "our shop in 1968"
    assert speech.tidy("1, 2, 3, 4 and then home") == "1, 2, 3, 4 and then home"


def test_a_language_outside_the_households_is_taken_as_its_first():
    assert speech._language(["ml", "ta", "ta"], ["ta", "en"]) == "ta"
    assert speech._language(["ml", "ml"], ["ta", "en"]) == "ta", "Tamil heard as Malayalam"
    assert speech._language(["<|en|>", "en"], ["ta", "en"]) == "en"
    assert speech._language([], []) == "en"


def _wav(path: Path, samples: np.ndarray, rate: int = speech.RATE, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def test_a_wav_is_read_without_ffmpeg_in_mono_at_sixteen_kilohertz(tmp_path):
    stereo = np.zeros(8000 * 2 * 2, dtype=np.float32)
    stereo[0::2] = 0.5
    samples = speech.read_samples(_wav(tmp_path / "s.wav", stereo, rate=8000, channels=2))
    assert len(samples) == 2 * speech.RATE
    assert abs(float(samples.mean()) - 0.25) < 0.01
    with pytest.raises(Unavailable):
        speech.read_samples(tmp_path / "story.webm")


class _Stream:
    def __init__(self, recogniser):
        self.recogniser = recogniser
        self.samples = None
        self.result = None

    def accept_waveform(self, rate, samples):
        self.samples = samples


class _Recogniser:
    """sherpa-onnx's Whisper recogniser, as far as speech.py uses it: it hears
    every piece as Malayalam, and answers in the language and task set."""

    def __init__(self, hears="ml"):
        self.hears = hears
        self.config = SimpleNamespace(model_config=SimpleNamespace(
            whisper=SimpleNamespace(language="", task="transcribe")))
        self.recognizer = SimpleNamespace(set_config=self._set)
        self.decoded: list[tuple[str, str]] = []

    def _set(self, config):
        self.config = config

    def create_stream(self):
        return _Stream(self)

    def decode_streams(self, streams):
        whisper = self.config.model_config.whisper
        for stream in streams:
            self.decoded.append((whisper.language, whisper.task))
            seconds = len(stream.samples) / speech.RATE
            if whisper.task == "translate":
                text = f"my father's shop ({seconds:.0f} s)"
            elif (whisper.language or self.hears) == "ta":
                text = f"அப்பாவின் கடை ({seconds:.0f} s)"
            else:
                text = "..."
            stream.result = SimpleNamespace(text=text, lang=whisper.language or self.hears)


def _kept(model="speech", hears="ml"):
    return {"sherpa": None, "recognizer": _Recogniser(hears), "model": model, "vad": None,
            "setting": (None, None)}


def test_a_tamil_story_is_written_down_and_translated(tmp_path):
    tone = np.sin(np.linspace(0, 2000, 40 * speech.RATE)).astype(np.float32) * 0.3
    path = _wav(tmp_path / "story.wav", tone)
    kept = _kept()
    result = speech.transcribe(kept, path=str(path), languages=["ta", "en"])
    assert result["language"] == "ta", "heard as Malayalam, kept to the household's Tamil"
    assert result["model"] == "speech" and result["version"] == speech.version_of("speech")
    assert result["seconds"] == 40.0
    assert [(s["start"], s["end"]) for s in result["segments"]] == [(0.0, 28.0), (28.0, 40.0)]
    assert result["text"] == "அப்பாவின் கடை (28 s) அப்பாவின் கடை (12 s)"
    assert result["english"] == "my father's shop (28 s) my father's shop (12 s)"
    tasks = [task for _lang, task in kept["recognizer"].decoded]
    assert tasks.count("translate") == 2 and tasks.count("transcribe") == 4


def test_a_story_heard_in_its_own_language_is_read_once(tmp_path):
    path = _wav(tmp_path / "story.wav", np.full(40 * speech.RATE, 0.2, dtype=np.float32))
    kept = _kept(hears="ta")
    result = speech.transcribe(kept, path=str(path), languages=["ta", "en"])
    assert result["language"] == "ta" and result["english"]
    assert kept["recognizer"].decoded == [("", "transcribe")] * 2 + [("ta", "translate")] * 2


def test_turbo_writes_tamil_without_a_translation(tmp_path):
    path = _wav(tmp_path / "story.wav", np.full(5 * speech.RATE, 0.2, dtype=np.float32))
    kept = _kept("speech-quick")
    result = speech.transcribe(kept, path=str(path), languages=["ta"])
    assert result["text"].startswith("அப்பாவின் கடை") and result["english"] == ""
    assert all(task == "transcribe" for _lang, task in kept["recognizer"].decoded)


def test_silence_is_nothing_to_write_down(tmp_path):
    path = _wav(tmp_path / "quiet.wav", np.zeros(0, dtype=np.float32))
    result = speech.transcribe(_kept(), path=str(path))
    assert result["segments"] == [] and result["text"] == ""


@pytest.mark.skipif(not os.environ.get("NINAIVU_TEST_WHISPER"),
                    reason="needs NINAIVU_TEST_WHISPER: an unpacked sherpa-onnx Whisper folder")
def test_the_real_reader_writes_down_a_recording(tmp_path):
    import sherpa_onnx
    folder = Path(os.environ["NINAIVU_TEST_WHISPER"])
    name = folder.name.replace("sherpa-onnx-whisper-", "")
    recogniser = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(folder / f"{name}-encoder.int8.onnx"),
        decoder=str(folder / f"{name}-decoder.int8.onnx"),
        tokens=str(folder / f"{name}-tokens.txt"), language="", task="transcribe")
    kept = {"sherpa": sherpa_onnx, "recognizer": recogniser, "model": "speech",
            "vad": os.environ.get("NINAIVU_TEST_VAD"), "setting": (None, None)}
    result = speech.transcribe(kept, path=str(folder / "test_wavs" / "0.wav"),
                               languages=["en", "ta"])
    assert result["language"] == "en"
    assert "yellow lamps" in result["text"].lower()


# ---------------------------------------------------------------------------
# The words, kept with their story
# ---------------------------------------------------------------------------

@pytest.fixture()
def told(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    conn.execute("INSERT INTO assets(root, rel_path, folder, filename, ext, kind, size, mtime) "
                 "VALUES('/lib', 'a.jpg', '', 'a.jpg', 'jpg', 'picture', 1, 0)")
    conn.commit()
    asset_id = conn.execute("SELECT id FROM assets").fetchone()[0]
    folder = tmp_path / "stories"
    story = stories.add(conn, folder, asset_id, b"RIFF....WAVE", mime="audio/wav",
                        created_by=None, speaker="Paati", duration=12.0)
    return conn, asset_id, story, folder


RESULT = {"language": "ta", "text": "இது என் அப்பாவின் கடை", "english": "This is my father's shop",
          "segments": [{"start": 0.0, "end": 3.5, "text": "இது என் அப்பாவின் கடை",
                        "english": "This is my father's shop"}],
          "model": "speech", "version": 12}


def test_a_story_is_owed_until_its_words_are_kept_and_then_searched(told):
    conn, asset_id, story, _ = told
    assert story_words.owed(conn, 12) == [story["id"]]
    assert stories.matching_asset_ids(conn, "father") == []
    assert story_words.store(conn, story["id"], RESULT)
    assert story_words.owed(conn, 12) == []
    assert story_words.owed(conn, 13) == [story["id"]], "a better model makes it owed again"
    assert stories.matching_asset_ids(conn, "father shop") == [asset_id], "found in English"
    assert stories.matching_asset_ids(conn, "அப்பாவின்") == [asset_id], "and in Tamil"
    assert stories.matching_asset_ids(conn, "paati") == [asset_id], "as before"
    words = story_words.get(conn, story["id"])
    assert words["language"] == "ta" and words["segments"][0]["end"] == 3.5


def test_a_story_that_fails_twice_is_set_aside_until_a_newer_model(told):
    conn, _, story, _ = told
    assert not story_words.fail(conn, story["id"], 12, "could not read")
    assert story_words.owed(conn, 12) == [story["id"]], "tried again once"
    assert story_words.fail(conn, story["id"], 12, "could not read")
    assert story_words.owed(conn, 12) == []
    assert story_words.get(conn, story["id"]) is None
    assert story_words.owed(conn, 13) == [story["id"]]
    assert not story_words.fail(conn, story["id"], 13, "again"), "a new model, a new count"


def test_the_words_go_with_their_story(told):
    conn, _, story, folder = told
    story_words.store(conn, story["id"], RESULT)
    stories.delete(conn, story["id"], folder)
    assert conn.execute("SELECT COUNT(*) FROM story_words").fetchone()[0] == 0
    assert not story_words.store(conn, story["id"], RESULT), "nothing kept for a story gone"


# ---------------------------------------------------------------------------
# Writing them down in the background
# ---------------------------------------------------------------------------

class _Host:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    def call(self, capability, **kwargs):
        self.calls.append({"capability": capability, **kwargs})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer)


def _transcriber(told, tmp_path, monkeypatch, answer, *, on=True, ready=None):
    conn, _, _, folder = told
    monkeypatch.setattr("ninaivu.runtime.transcriber.ready", lambda: ready)
    monkeypatch.setattr(speech, "chosen_model", lambda: "speech")
    cfg = SimpleNamespace(ai_runtime=on, state_dir=str(folder.parent), speech_languages="ta,en")
    host = _Host(answer)
    return Transcriber(cfg, lambda: conn, host=lambda: host, hold=lambda: None), host


def test_the_engine_writes_down_every_story_owed(told, tmp_path, monkeypatch):
    conn, asset_id, story, _ = told
    transcriber, host = _transcriber(told, tmp_path, monkeypatch, RESULT)
    state = transcriber.run()
    assert state["done"] == 1 and state["owed"] == 0
    call = host.calls[0]
    assert call["capability"] == "transcribe" and call["languages"] == ["ta", "en"]
    assert call["path"].endswith(story["file"]) and call["timeout"] >= 600
    assert stories.matching_asset_ids(conn, "father") == [asset_id]
    host.calls.clear()
    transcriber.run()
    assert host.calls == [], "nothing is written down twice"


def test_a_failure_is_counted_against_the_story(told, tmp_path, monkeypatch):
    conn, _, story, _ = told
    transcriber, _ = _transcriber(told, tmp_path, monkeypatch, HostCrashed("gone"))
    assert transcriber.run()["failed"] == 1
    row = conn.execute("SELECT failures, error FROM story_words").fetchone()
    assert tuple(row) == (1, "gone")


def test_a_missing_model_ends_the_pass_and_blames_no_story(told, tmp_path, monkeypatch):
    conn, _, _, _ = told
    transcriber, _ = _transcriber(told, tmp_path, monkeypatch, Unavailable("no model"))
    state = transcriber.run()
    assert state["message"] == "no model" and state["failed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM story_words").fetchone()[0] == 0


def test_without_the_reader_nothing_is_asked(told, tmp_path, monkeypatch):
    transcriber, host = _transcriber(told, tmp_path, monkeypatch, RESULT,
                                     ready="the speech reader is not installed")
    assert transcriber.run()["message"] == "the speech reader is not installed"
    assert host.calls == []


def test_the_household_comes_first(told, tmp_path, monkeypatch):
    transcriber, host = _transcriber(told, tmp_path, monkeypatch, RESULT)
    holds = iter(["a video is playing", None])
    transcriber._hold = lambda: next(holds)
    monkeypatch.setattr(transcriber._stop, "wait", lambda seconds: False)
    assert transcriber.run()["done"] == 1


# ---------------------------------------------------------------------------
# In the app
# ---------------------------------------------------------------------------

def test_the_page_is_given_the_words_of_each_story(as_family, people, app):
    from test_voice_stories import first_id, tell
    asset_id = first_id(as_family)
    story = tell(as_family, asset_id, speaker="Paati").get_json()["story"]
    assert story["words"] is None, "nothing until the engine has written it down"
    story_words.store(people["conn"], story["id"], RESULT)
    listed = as_family.get(f"/api/asset/{asset_id}/stories").get_json()["stories"]
    assert listed[0]["words"]["english"] == "This is my father's shop"
    assert listed[0]["words"]["segments"][0]["text"] == "இது என் அப்பாவின் கடை"
    found = as_family.get("/api/assets?q=father&plain=1").get_json()["items"]
    assert [i["id"] for i in found] == [asset_id]


def test_telling_a_story_wakes_the_engine(as_family, app):
    from test_voice_stories import first_id, tell
    transcriber = app.config["MV_SERVICES"].transcriber
    transcriber._wake.clear()
    tell(as_family, first_id(as_family))
    assert transcriber._wake.is_set()


def test_the_switch_is_on_the_advanced_page_and_off_by_default(scanned):
    from test_settings_groups import _console
    client, cfg = _console(scanned)
    assert cfg.ai_runtime is False
    groups = client.get("/api/admin/settings/all").get_json()["groups"]
    advanced = next(g for g in groups if g["name"] == "Advanced")
    assert "ai_runtime" in json.dumps(advanced)
    response = client.post("/api/admin/settings/all", json={"settings": {"ai_runtime": True}})
    assert response.status_code == 200 and response.get_json()["restart"] == []
    assert cfg.ai_runtime is True


def test_switching_it_off_stops_the_model_process(scanned):
    from test_settings_groups import _console
    client, cfg = _console(scanned)
    services = client.application.config["MV_SERVICES"]
    services.model_host()
    assert services._model_host is not None
    client.post("/api/admin/settings/all", json={"settings": {"ai_runtime": False}})
    client.post("/api/admin/settings/all", json={"settings": {"ai_runtime": True}})
    client.post("/api/admin/settings/all", json={"settings": {"ai_runtime": False}})
    assert services._model_host is None


def test_the_drive_copy_of_the_index_carries_no_words(as_family, scanned, people, tmp_path):
    import sqlite3
    from ninaivu.cloud import index_copy
    from test_voice_stories import first_id, tell
    cfg, _, _ = scanned
    story = tell(as_family, first_id(as_family)).get_json()["story"]
    story_words.store(people["conn"], story["id"], RESULT)
    bundle = index_copy.make_bundle(cfg.state_dir, tmp_path / "out")
    with tarfile.open(bundle) as tar:
        member = next(n for n in tar.getnames() if n.endswith("index.db"))
        tar.extract(member, tmp_path / "x")
    index = next((tmp_path / "x").rglob("index.db"))
    copied = sqlite3.connect(str(index))
    assert copied.execute("SELECT COUNT(*) FROM story_words").fetchone()[0] == 0
    copied.close()
    assert "அப்பாவின்".encode() not in index.read_bytes()


def test_the_image_models_run_on_the_processor_on_a_mac_until_core_ml_matches():
    """Core ML's default format gave SigLIP 2 vectors only 0.80 to 0.95 alike
    PyTorch's on an M1; the ML Program ways are there to be measured."""
    offered = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert devices.image_providers(None, offered, platform="darwin") == ["CPUExecutionProvider"]
    coreml = devices.image_providers("coreml-gpu", offered, platform="darwin")
    assert coreml[0] == ("CoreMLExecutionProvider",
                         {"ModelFormat": "MLProgram", "MLComputeUnits": "CPUAndGPU"})
    assert coreml[-1] == "CPUExecutionProvider"
    assert devices.image_providers("coreml", ["CPUExecutionProvider"], platform="darwin") == [
        "CPUExecutionProvider"], "asked for, not there: the processor"
    assert devices.image_providers("auto", ["DmlExecutionProvider", "CPUExecutionProvider"],
                                   platform="win32")[0] == "DmlExecutionProvider"
