"""Speaking to search: a few seconds said aloud, written down on this computer
by the voice-story model, searched as if typed, and never kept."""
from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from ninaivu import runtime
from ninaivu.api import api_voice_search
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS, OFF_THE_PAGE


def _wav(seconds: float = 0.5) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return out.getvalue()


class FakeHost:
    def __init__(self, heard):
        self.heard, self.calls = heard, []

    def call(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        assert Path(kwargs["path"]).is_file(), "the recording is there while it is read"
        self.path = Path(kwargs["path"])
        if isinstance(self.heard, Exception):
            raise self.heard
        return dict(self.heard)


@pytest.fixture()
def listening(app, scanned, monkeypatch):
    cfg, _, _ = scanned
    cfg.voice_search = True
    monkeypatch.setattr(api_voice_search, "ready", lambda: None)

    def hear(heard):
        host = FakeHost(heard)
        monkeypatch.setattr(api_voice_search, "_host", lambda: host)
        return host
    return {"cfg": cfg, "hear": hear}


def say(client, data=None, content_type="audio/wav"):
    return client.post("/api/voice-search", data=_wav() if data is None else data,
                       content_type=content_type)


def test_off_by_default_with_the_advanced_settings(as_family):
    assert Config().voice_search is True                 # on by default since 3.1
    assert "voice_search" in GROUPS["Advanced"] and "voice_search" in OFF_THE_PAGE
    assert as_family.get("/api/voice-search").status_code == 404
    assert say(as_family).status_code == 404
    assert as_family.get("/api/status").get_json()["moments"]["voice"] is False


def test_an_english_search_is_searched_as_said(as_family, listening):
    host = listening["hear"]({"text": "Paati at the beach", "english": "", "language": "en",
                              "took": 1.2})
    assert as_family.get("/api/status").get_json()["moments"]["voice"] is True
    got = say(as_family)
    assert got.status_code == 200, got.get_json()
    body = got.get_json()
    assert body["query"] == "Paati at the beach" and body["language"] == "en"
    capability, kwargs = host.calls[0]
    assert capability == "transcribe" and kwargs["translate"] is True
    assert kwargs["languages"] == ["ta", "en"] and kwargs["wait"] == api_voice_search.HOST_WAIT
    assert not host.path.exists(), "a spoken search is never kept"


def test_a_tamil_search_is_searched_in_english_and_shows_what_was_heard(as_family, listening):
    listening["hear"]({"text": "பாட்டி கடற்கரையில்", "english": "Grandmother at the beach",
                       "language": "ta"})
    body = say(as_family).get_json()
    assert body["query"] == "Grandmother at the beach"
    assert body["text"] == "பாட்டி கடற்கரையில்"


def test_tamil_without_a_translation_is_searched_in_tamil():
    assert api_voice_search.query_for({"text": "கோயில்", "english": "", "language": "ta"}) == "கோயில்"
    assert api_voice_search.query_for({"text": "", "english": "", "language": ""}) == ""


def test_what_is_not_a_recording_is_refused(as_family, listening):
    listening["hear"]({"text": "x", "language": "en"})
    assert say(as_family, data=b"").status_code == 400
    assert say(as_family, data=b"<html>not sound</html>", content_type="audio/webm").status_code == 415
    big = b"RIFF" + b"\x00" * (api_voice_search.MAX_BYTES + 10)
    assert say(as_family, data=big).status_code == 413


@pytest.mark.parametrize("failure, status", [
    (runtime.HostBusy("busy"), 429),
    (runtime.Unavailable("no speech model is downloaded (AI models page)"), 409),
    (runtime.HostError("garbled"), 422),
])
def test_a_host_that_cannot_answer_is_said_and_the_recording_still_goes(as_family, listening,
                                                                     failure, status):
    host = listening["hear"](failure)
    response = say(as_family)
    assert response.status_code == status and response.get_json()["error"]
    assert not host.path.exists()


def test_not_ready_says_why_before_anybody_speaks(as_family, listening, monkeypatch):
    monkeypatch.setattr(api_voice_search, "ready",
                        lambda: "no speech model is downloaded (AI models page)")
    status = as_family.get("/api/voice-search").get_json()
    assert status["ready"] is False and "AI models" in status["why"]
    assert say(as_family).status_code == 409


def test_a_guest_types(as_guest, listening):
    assert as_guest.get("/api/voice-search").status_code in (403, 404)
    assert say(as_guest).status_code in (403, 404)


def test_the_page_has_a_microphone_that_records_only_when_pressed():
    root = Path(__file__).resolve().parents[1] / "ninaivu"
    page = (root / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="voice-search"' in page and "hidden" in page.split('id="voice-search"')[1][:120]
    script = (root / "static" / "js" / "voice-search.js").read_text(encoding="utf-8")
    # The microphone is asked for on the press, and let go of when it stops.
    assert script.index("getUserMedia") > script.index("async start()")
    assert "getTracks().forEach((track) => track.stop())" in script
    assert "/api/voice-search" in script


def test_the_hosts_own_words_are_not_handed_back(as_family, listening):
    listening["hear"](runtime.Unavailable("Traceback (most recent call last): /secret/path.py"))
    body = say(as_family).get_json()
    assert "Traceback" not in body["error"] and "AI models" in body["error"]
