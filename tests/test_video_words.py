"""Words spoken in videos: search finds the second something was said.

Off until switched on; every home video is listened to once by the speech
model that writes down voice stories, a window at a time, and each line is
kept with the second it starts and ends at; a search answers with the videos
and those lines, only those the viewer may see, and the viewer is given them
as subtitles; Whisper's words for music are left out; a video hidden from AI
loses its words, one changed on disk or heard by a better model is listened
to again, and voice stories go first.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ninaivu.media import media
from ninaivu.media import video_words as listener_mod
from ninaivu.runtime import speech
from ninaivu.runtime.host import HostCrashed, HostStopped, Unavailable
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS, OFF_THE_PAGE
from ninaivu.storage import db, video_words

needs_ffmpeg = pytest.mark.skipif(not media.FFMPEG, reason="ffmpeg is not installed")

VERSION = video_words.wanted_version(speech.version_of("speech"))


def _clip(path: Path, seconds: int, *, sound: bool = True) -> None:
    inputs = ["-f", "lavfi", "-i", f"color=c=orange:size=160x120:duration={seconds}:rate=5"]
    if sound:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    subprocess.run([media.FFMPEG, "-v", "error", "-y", *inputs, "-shortest",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=60)


class Host:
    """The model host, standing in for Whisper: the lines said in each window
    it was asked for, Tamil with English, and nothing from a silent video."""

    SAID = {0.0: ("பிறந்தநாள் வாழ்த்துக்கள் கண்ணா", "Happy birthday, kanna"),
            10.0: ("Thank you for watching", ""),
            20.0: ("கேக் வெட்டு", "Cut the cake")}

    def __init__(self, answer=None):
        self.calls: list[dict] = []
        self.answer = answer

    def call(self, capability, **kwargs):
        self.calls.append({"capability": capability, **kwargs})
        if isinstance(self.answer, BaseException):
            raise self.answer
        start, length = float(kwargs["offset"]), float(kwargs["length"])
        if "silent" in kwargs["path"]:
            return {"language": "", "segments": [], "text": "", "english": ""}
        segments = [{"start": at, "end": at + 4, "text": text, "english": english}
                    for at, (text, english) in self.SAID.items() if start <= at < start + length]
        return {"language": "ta", "segments": segments, "text": "", "english": ""}


@pytest.fixture()
def clips(app, people, scanned, monkeypatch):
    """A birthday video (30 s, with sound), a silent one (10 s) and one too
    short to listen to (2 s), indexed; the speech model there; the switches on."""
    if not media.FFMPEG:
        pytest.skip("ffmpeg is not installed")
    cfg, conn, scanner = scanned
    folder = Path(cfg.active_root) / "videos"
    folder.mkdir()
    _clip(folder / "birthday.mp4", 30)
    _clip(folder / "silent.mp4", 10, sound=False)
    _clip(folder / "blink.mp4", 2)
    scanner._run(Path(cfg.active_root), full=False)
    ids = {r["filename"]: int(r["id"]) for r in conn.execute(
        "SELECT id, filename FROM assets WHERE kind='video'")}
    assert set(ids) == {"birthday.mp4", "silent.mp4", "blink.mp4"}
    monkeypatch.setattr("ninaivu.runtime.transcriber.ready", lambda: None)
    monkeypatch.setattr(speech, "chosen_model", lambda: "speech")
    cfg.video_words = True
    cfg.ai_runtime = True
    host = Host()
    listener = listener_mod.WordListener(cfg, lambda: db.connect(cfg.db_path),
                                         host=lambda: host)
    return {"ids": ids, "conn": conn, "cfg": cfg, "host": host, "listener": listener,
            "birthday": ids["birthday.mp4"], "silent": ids["silent.mp4"]}


def test_off_by_default_and_under_advanced():
    assert Config().video_words is True                 # on by default since 3.1
    assert "video_words" in GROUPS["Advanced"] and "video_words" in OFF_THE_PAGE


def test_every_route_is_404_until_switched_on(as_family):
    assert as_family.get("/api/video-words?q=cake").status_code == 404
    assert as_family.get("/api/video-words/1").status_code == 404
    assert as_family.get("/api/video-words/1.vtt").status_code == 404
    assert as_family.get("/api/video-words/1/frame?at=0").status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["video_words"] is False


def test_a_long_video_is_heard_in_windows():
    assert listener_mod.windows(30) == [(0.0, 30)]
    assert listener_mod.windows(1500) == [(0.0, 600.0), (600.0, 600.0), (1200.0, 300.0)]
    # A last minute or less goes with the window before it.
    assert listener_mod.windows(1230) == [(0.0, 600.0), (600.0, 630.0)]


def test_lines_leave_out_what_whisper_writes_for_music():
    lines = video_words.lines_of([
        {"start": 3, "end": 5, "text": "  Happy   birthday ", "english": ""},
        {"start": 9, "end": 12, "text": "Thank you for watching!", "english": ""},
        {"start": 1, "end": 2, "text": "[Music]", "english": "♪"},
        {"start": 14, "end": 13, "text": "", "english": "Cut the cake"},
    ], "en")
    assert [(line["at"], line["end"], line["text"], line["english"]) for line in lines] == [
        (3.0, 5.0, "Happy birthday", ""), (14.0, 14.0, "", "Cut the cake")]
    assert lines[0]["language"] == "en"


def test_subtitles_in_the_language_said_and_in_english():
    lines = [{"at": 1.5, "end": 4.25, "language": "ta", "text": "கேக் வெட்டு",
              "english": "Cut the cake"},
             {"at": 3725, "end": 3725, "language": "en", "text": "Blow --> the candles",
              "english": ""}]
    spoken = video_words.vtt(lines)
    assert spoken.startswith("WEBVTT\n\n1\n00:00:01.500 --> 00:00:04.250\nகேக் வெட்டு\n")
    assert "01:02:05.000 --> 01:02:05.500\nBlow -> the candles" in spoken
    english = video_words.vtt(lines, "english")
    assert "Cut the cake" in english and "Blow -> the candles" in english
    assert "கேக்" not in english


@needs_ffmpeg
def test_a_window_of_the_sound_and_none_from_a_silent_video(tmp_path):
    _clip(tmp_path / "s.mp4", 6)
    _clip(tmp_path / "q.mp4", 4, sound=False)
    whole = speech.read_samples(tmp_path / "s.mp4", media.FFMPEG)
    part = speech.read_samples(tmp_path / "s.mp4", media.FFMPEG, offset=2, length=3)
    assert abs(len(whole) / speech.RATE - 6) < 0.2
    assert abs(len(part) / speech.RATE - 3) < 0.1
    assert len(speech.read_samples(tmp_path / "q.mp4", media.FFMPEG)) == 0


def test_every_video_is_written_down_once(clips):
    state = clips["listener"].run()
    assert state["done"] == 2 and state["failed"] == 0 and state["owed"] == 0
    conn, birthday = clips["conn"], clips["birthday"]
    lines = video_words.of_clip(conn, birthday)
    assert [(line["at"], line["english"]) for line in lines] == [(0.0, "Happy birthday, kanna"),
                                                       (20.0, "Cut the cake")]
    assert lines[0]["language"] == "ta"
    call = clips["host"].calls[0]
    assert call["capability"] == "transcribe" and call["translate"] is True
    assert call["languages"] == ["ta", "en"] and call["offset"] == 0.0
    assert call["timeout"] >= 600
    assert {Path(c["path"]).name for c in clips["host"].calls} == {"birthday.mp4", "silent.mp4"}
    assert video_words.counts(conn) == {"clips": 2, "lines": 2}
    clips["host"].calls.clear()
    clips["listener"].run()
    assert clips["host"].calls == [], "nothing is listened to twice"


def test_a_search_opens_the_video_where_it_was_said(clips, as_family):
    clips["listener"].run()
    birthday = clips["birthday"]
    for words in ("cake", "CUT the", "வெட்டு", "கண்ணா birthday"):
        got = as_family.get(f"/api/video-words?q={words}").get_json()
        assert [c["id"] for c in got["clips"]] == [birthday], words
    got = as_family.get("/api/video-words?q=cake").get_json()
    assert got["looked_clips"] == 2 and got["clips"][0]["name"] == "birthday.mp4"
    assert [line["at"] for line in got["clips"][0]["lines"]] == [20.0]
    assert as_family.get("/api/video-words?q=watching").get_json()["clips"] == []
    assert as_family.get("/api/video-words?q=cake candles").get_json()["clips"] == []
    assert as_family.get("/api/video-words?q=").get_json()["clips"] == []
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["video_words"] is True


def test_the_viewer_is_given_the_words_as_subtitles(clips, as_family):
    clips["listener"].run()
    birthday = clips["birthday"]
    got = as_family.get(f"/api/video-words/{birthday}").get_json()
    assert got["languages"] == ["ta"] and got["english"] is True and len(got["lines"]) == 2
    spoken = as_family.get(f"/api/video-words/{birthday}.vtt")
    assert spoken.status_code == 200 and spoken.mimetype == "text/vtt"
    assert "கேக் வெட்டு" in spoken.get_data(as_text=True)
    english = as_family.get(f"/api/video-words/{birthday}.vtt?lang=english")
    assert "Cut the cake" in english.get_data(as_text=True)
    assert as_family.get(f"/api/video-words/{clips['silent']}").get_json()["lines"] == []


def test_a_picture_only_where_a_line_starts(clips, as_family):
    clips["listener"].run()
    birthday = clips["birthday"]
    picture = as_family.get(f"/api/video-words/{birthday}/frame?at=20")
    assert picture.status_code == 200 and picture.data[:2] == b"\xff\xd8"
    assert as_family.get(f"/api/video-words/{birthday}/frame?at=7").status_code == 404
    assert as_family.get(f"/api/video-words/{birthday}/frame?at=x").status_code == 400


def test_only_what_the_viewer_may_see(clips, people, as_family, as_admin):
    clips["listener"].run()
    conn, birthday = clips["conn"], clips["birthday"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET visibility=2, private_to=? WHERE id=?",
                     (people["admin"].id, birthday))
        conn.commit()
    assert as_family.get("/api/video-words?q=cake").get_json()["clips"] == []
    assert as_family.get(f"/api/video-words/{birthday}").status_code == 404
    assert as_family.get(f"/api/video-words/{birthday}.vtt").status_code == 404
    assert as_family.get(f"/api/video-words/{birthday}/frame?at=20").status_code == 404
    assert [c["id"] for c in as_admin.get("/api/video-words?q=cake").get_json()["clips"]] \
        == [birthday]


def test_hidden_from_ai_forgets_its_words_and_is_not_heard_again(clips):
    clips["listener"].run()
    conn, birthday = clips["conn"], clips["birthday"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (birthday,))
        conn.commit()
    db.forget_ai_reading(conn, ids=[birthday])
    assert video_words.of_clip(conn, birthday) == []
    assert video_words.count_owed(conn, VERSION) == 0
    clips["listener"].run()
    assert video_words.of_clip(conn, birthday) == []


def test_a_video_changed_on_disk_or_a_better_model_hears_it_again(clips):
    clips["listener"].run()
    conn, birthday = clips["conn"], clips["birthday"]
    assert video_words.count_owed(conn, VERSION) == 0
    assert video_words.count_owed(conn, VERSION + 1) == 2
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET mtime=mtime+10 WHERE id=?", (birthday,))
        conn.commit()
    assert video_words.count_owed(conn, VERSION) == 1
    clips["listener"].run()
    assert video_words.count_owed(conn, VERSION) == 0


def test_a_video_that_cannot_be_heard_is_set_aside_after_two_tries(clips):
    clips["host"].answer = HostCrashed("broken sound")
    conn = clips["conn"]
    assert clips["listener"].run()["failed"] == 2
    assert video_words.count_owed(conn, VERSION) == 2
    clips["listener"].run()
    assert video_words.count_owed(conn, VERSION) == 0
    row = conn.execute("SELECT failures, error FROM video_words_done "
                       "WHERE asset_id=?", (clips["birthday"],)).fetchone()
    assert tuple(row) == (2, "broken sound")


def test_a_missing_model_or_a_stop_blames_no_video(clips):
    conn = clips["conn"]
    clips["host"].answer = Unavailable("no speech model is downloaded")
    state = clips["listener"].run()
    assert state["message"] == "no speech model is downloaded" and state["failed"] == 0
    clips["host"].answer = HostStopped("stopped")
    assert clips["listener"].run()["failed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM video_words_done").fetchone()[0] == 0


def test_without_the_engine_or_the_reader_nothing_is_asked(clips, monkeypatch):
    clips["cfg"].ai_runtime = False
    assert not clips["listener"].enabled
    clips["cfg"].ai_runtime = True
    monkeypatch.setattr("ninaivu.runtime.transcriber.ready",
                        lambda: "the speech reader is not installed")
    assert clips["listener"].run()["message"] == "the speech reader is not installed"
    assert clips["host"].calls == []


def test_it_waits_for_the_household_and_the_voice_stories(clips):
    cfg, conn = clips["cfg"], clips["conn"]
    calls = []

    def hold():
        calls.append(1)
        if len(calls) > 2:
            listener.stop()
        return "voice stories are being written down first"

    listener = listener_mod.WordListener(cfg, lambda: db.connect(cfg.db_path),
                                         host=lambda: clips["host"], hold=hold)
    listener._stop.wait = lambda seconds: False                     # noqa: SLF001
    listener.run()
    assert clips["host"].calls == []
    assert video_words.of_clip(conn, clips["birthday"]) == []


def test_the_services_hold_it_while_a_story_is_written_down(app):
    services = app.config["MV_SERVICES"]
    assert services.word_listener is not None
    services.transcriber._state["running"] = True                   # noqa: SLF001
    try:
        reason = services._video_words_hold()                       # noqa: SLF001
        assert reason and ("voice stories" in reason or reason == services.workload.hold("analysis"))
    finally:
        services.transcriber._state["running"] = False              # noqa: SLF001


def test_deleting_the_video_takes_its_words(clips):
    clips["listener"].run()
    conn, birthday = clips["conn"], clips["birthday"]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("DELETE FROM assets WHERE id=?", (birthday,))
        conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM video_words").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM video_words_done "
                        "WHERE asset_id=?", (birthday,)).fetchone()[0] == 0
