"""Films, TV and music stay out of the family archive; recordings and home video never do.

The same old drive that holds downloaded courses holds downloaded songs and
films, often loose beside the family's own voice notes and phone videos. These
check that entertainment is recognised file by file from names and tags, and
held back until someone decides -- and, above all, that anything personal is
copied however it is named: a phone's video, a voice note, a recording that
sounds like speech.
"""
import math
import os
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ninaivu.archive import entertainment, scanner, status_kit          # noqa: E402
from ninaivu.archive import database as db                               # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                           # noqa: E402
from test_archive_course_material import archived_names, video, write     # noqa: E402
from test_archive_course_material import drive as drive                   # noqa: E402,F401

RATE = 16000


# ---------------------------------------------------------------------------
# Sounds
# ---------------------------------------------------------------------------

def _wav(path, samples, channels=1, rate=RATE, tags=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(samples, -1, 1)
    if channels == 2:
        data = np.repeat(data, 2)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((data * 32767).astype("<i2").tobytes())
    if tags:
        from mutagen.wave import WAVE
        audio = WAVE(str(path))
        audio.add_tags()
        for frame in tags:
            audio.tags.add(frame)
        audio.save()
    return path


def music_sound(seconds=6, rate=RATE, root=220):
    """A chord that never stops, with a beat: a song, as far as pauses go."""
    t = np.arange(int(seconds * rate)) / rate
    chord = sum(np.sin(2 * math.pi * root * f * t) for f in (1, 1.26, 1.5, 2)) / 4
    beat = 0.75 + 0.25 * np.sin(2 * math.pi * 2 * t)
    return 0.6 * chord * beat


def speech_sound(seconds=6, rate=RATE, seed=3):
    """Syllables with gaps between words: the pauses of somebody talking."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * rate), dtype=np.float64)
    at = 0
    while at < len(out):
        syllables = rng.integers(2, 5)
        for _ in range(syllables):
            length = int(rate * rng.uniform(0.12, 0.22))
            t = np.arange(length) / rate
            voice = np.sin(2 * math.pi * rng.uniform(110, 180) * t) + 0.3 * rng.standard_normal(length)
            chunk = 0.5 * voice * np.hanning(length)
            end = min(len(out), at + length)
            out[at:end] = chunk[:end - at]
            at += length + int(rate * 0.04)
        at += int(rate * rng.uniform(0.3, 0.6))
    return out


def song_tags(site="MassTamilan.com"):
    from mutagen import id3
    return [id3.TALB(encoding=3, text=f"Nadodi Thendral - {site}"),
            id3.TPE1(encoding=3, text="Ilaiyaraaja"),
            id3.TRCK(encoding=3, text="3"),
            id3.COMM(encoding=3, lang="eng", desc="", text=f"{site} - Download 320kbps")]


def a_song(folder, name, sound=None, tags=True):
    """A CD-quality stereo download; distinct, or the archive keeps one as a duplicate."""
    if sound is None:
        sound = music_sound(rate=44100, root=200 + sum(map(ord, name)) % 200)
    return _wav(folder / name, sound, channels=2, rate=44100, tags=song_tags() if tags else None)


# ---------------------------------------------------------------------------
# Listening
# ---------------------------------------------------------------------------

def test_listening_tells_speech_from_music(tmp_path):
    song = _wav(tmp_path / "song.wav", music_sound(rate=44100), channels=2, rate=44100)
    talk = _wav(tmp_path / "talk.wav", speech_sound())
    silence = _wav(tmp_path / "quiet.wav", np.zeros(RATE * 5))
    assert entertainment.listen(str(song), 6)[0] == "music"
    assert entertainment.listen(str(talk), 6)[0] == "speech"
    assert entertainment.listen(str(silence), 5)[0] == "speech", "undecidable counts as speech"


def test_listening_works_on_a_wav_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(entertainment, "FFMPEG", None)
    talk = _wav(tmp_path / "talk.wav", speech_sound(), channels=2, rate=44100)
    assert entertainment.listen(str(talk), 6)[0] == "speech"
    assert entertainment.listen(str(tmp_path / "missing.mp3"), 6) is None


# ---------------------------------------------------------------------------
# Judging names, tags and measurements
# ---------------------------------------------------------------------------

def judge(files, folder="Downloads", subdirs=(), tags=None, probe=None, heard=("music", "")):
    kinds = {"mkv": "video", "mp4": "video", "mov": "video", "mp3": "audio", "m4a": "audio",
             "wav": "audio", "opus": "audio", "amr": "audio", "jpg": "image"}
    return entertainment.judge_folder(
        folder, list(subdirs), list(files),
        kind_of=lambda n: kinds.get(n.rsplit(".", 1)[-1].lower()),
        size_of=lambda path: 900 * 1024 * 1024 if path.endswith((".mkv", ".mp4", ".mov")) else 5_000_000,
        tags=lambda path: (tags or {}).get(os.path.basename(path), {"channels": 2, "sample_rate": 44100}),
        probe=lambda path: (probe or {}).get(os.path.basename(path)),
        listen_to=lambda path, length: heard)


def verdict(judged, name):
    return next((v for v in judged.files if v.name == name), None)


@pytest.mark.parametrize("name", [
    "Kaithi (2019) Tamil HDRip x264 AAC 700MB ESub - TamilRockers.mkv",
    "The.Family.Man.S01E03.1080p.WEB-DL.mkv",
    "Vikram.2022.720p.BluRay.x265.mkv",
    "[www.1TamilMV.cx] - Leo (2023) Tamil.mp4",
])
def test_films_and_episodes_are_recognised_by_name(name):
    found = verdict(judge([name]), name)
    assert found and found.held, found


def test_songs_are_recognised_by_their_site_and_tags():
    tagged = {"Oru-Ganam.mp3": {"channels": 2, "sample_rate": 44100, "album": "Nadodi Thendral",
                                "artist": "Ilaiyaraaja", "track": "2",
                                "text": ["Nadodi Thendral - MassTamilan.com"]}}
    judged = judge(["Maniye-Manikuyile-MassTamilan.com.mp3", "Oru-Ganam.mp3"], tags=tagged)
    assert verdict(judged, "Maniye-Manikuyile-MassTamilan.com.mp3").held
    oru = verdict(judged, "Oru-Ganam.mp3")
    assert oru.held and any("music site in the tags" in r for r in oru.reasons), oru.reasons


@pytest.mark.parametrize("name", [
    "IMG_0768.MOV", "VID_20190607_123456.mp4", "Meeting with Acme.com.mp4",
    "115 WhatsTheTime.com.mp4", "mnd-nwuo-tjs (2026-08-11 14_58 GMT).mp4",
    "Priya wedding 1080p.mp4", "Grandpa stories (2019).mp4",
])
def test_home_video_and_recordings_are_not_suspected(name):
    assert verdict(judge([name]), name) is None


def test_measurements_tip_a_named_film_but_never_accuse_a_plain_name():
    wide = {"streams": [{"codec_type": "video", "width": 1920, "height": 800},
                        {"codec_type": "audio", "tags": {"language": "tam"}},
                        {"codec_type": "audio", "tags": {"language": "tel"}},
                        {"codec_type": "subtitle"}],
            "format": {"duration": "9000"}}
    judged = judge(["Final.mp4", "Kaithi (2019) x264.mp4"],
                   probe={"Final.mp4": wide, "Kaithi (2019) x264.mp4": wide})
    assert verdict(judged, "Final.mp4") is None, "a plain name is never accused"
    assert verdict(judged, "Kaithi (2019) x264.mp4").held


def test_a_camera_inside_the_file_or_a_family_occasion_spares_it():
    phone = {"format": {"tags": {"com.apple.quicktime.make": "Apple",
                                 "com.apple.quicktime.model": "iPhone 16 Pro Max"}},
             "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]}
    cinematic = {"streams": [{"codec_type": "video", "width": 3840, "height": 1608}],
                 "format": {"duration": "7200"}}
    judged = judge(["Movie night x264 (2021).mp4", "Priya weds Rajesh (2019) x264.mp4"],
                   probe={"Movie night x264 (2021).mp4": phone,
                          "Priya weds Rajesh (2019) x264.mp4": cinematic})
    night = verdict(judged, "Movie night x264 (2021).mp4")
    assert night.suspected and not night.held and "iPhone" in night.personal
    wedding = verdict(judged, "Priya weds Rajesh (2019) x264.mp4")
    assert wedding.suspected and not wedding.held and "weds" in wedding.personal


def test_an_occasion_does_not_hide_a_release():
    name = "The Wedding Guest 2018 1080p BluRay x264.mkv"
    assert verdict(judge([name]), name).held


@pytest.mark.parametrize("name,tags", [
    ("PTT-20190607-WA0003-MassTamilan.com.opus", None),
    ("REC_0012 - MassTamilan.com.wav", None),
    ("Voice 003 lyrics MassTamilan.com.m4a", None),
    ("Amma singing - MassTamilan.com.m4a", {"channels": 1, "sample_rate": 48000}),
    ("Call - MassTamilan.com.mp3", {"channels": 2, "sample_rate": 8000}),
    ("Pravachan - MassTamilan.com.mp3", {"channels": 2, "sample_rate": 44100, "genre": "Speech"}),
])
def test_recordings_are_spared_however_they_are_named(name, tags):
    judged = judge([name], tags={name: tags} if tags else None)
    found = verdict(judged, name)
    assert found and found.suspected and not found.held and found.personal, found


def test_audio_that_sounds_like_speech_is_spared():
    name = "Kamba Ramayanam discourse - Friendstamilmp3.com.mp3"
    judged = judge([name], heard=("speech", "sounds like speech"))
    assert not verdict(judged, name).held
    assert verdict(judge([name]), name).held


def test_an_album_folder_tips_its_tracks_and_takes_its_cover_with_them():
    tracks = [f"0{i} Song {i} (Lyrics).mp3" for i in range(1, 6)]
    tagged = {t: {"channels": 2, "sample_rate": 44100} for t in tracks}
    judged = judge(tracks + ["cover.jpg", "Nadodi Thendral.cue", "Our trip.jpg"],
                   folder="Nadodi Thendral", tags=tagged)
    assert all(verdict(judged, t).held for t in tracks), [verdict(judged, t) for t in tracks]
    assert judged.artwork == {"music": ["cover.jpg"]}


def test_a_lone_song_with_only_wording_is_left_alone_but_not_among_songs():
    lone = "Un+Paarvayil+with+Lyrics+Tamil+HD+Audio.mp3"
    assert verdict(judge([lone]), lone) is None
    among = [lone] + [f"Song{i}-MassTamilan.com.mp3" for i in range(4)]
    assert verdict(judge(among), lone).held


# ---------------------------------------------------------------------------
# What a run does with them
# ---------------------------------------------------------------------------

def a_download_folder(root):
    folder = root / "Phone" / "Download"
    for title in ("Maniye-Manikuyile", "Oru-Ganam", "Santhana-Marbile"):
        a_song(folder, f"{title}-MassTamilan.com.wav")
    _wav(folder / "11-02-21-20-36-23.wav", speech_sound())                    # a voice note
    a_song(folder, "Ramayanam discourse-MassTamilan.com.wav", sound=speech_sound(rate=44100))
    write(folder / "Kaithi (2019) Tamil HDRip x264 - TamilRockers.mp4", video())
    write(folder / "VID_20190607_123456.mp4", video())
    return folder


def by_category(job):
    return {e["category"]: e for e in job.entertainment}


def test_songs_and_films_are_held_back_and_recordings_arrive(drive):
    root, dest = drive
    a_download_folder(root)

    job = ArchiveJob([str(root)], str(dest))
    job.run()

    names = archived_names(dest)
    assert sorted(names) == sorted(["11-02-21-20-36-23.wav", "Ramayanam discourse-MassTamilan.com.wav",
                                    "VID_20190607_123456.mp4"]), names
    music, film = by_category(job)["music"], by_category(job)["film"]
    assert music["state"] == "held" and music["files"] == 3
    assert music["spared"] == 1 and "speech" in music["spared_reasons"][0]
    assert film["state"] == "held" and film["files"] == 1
    message = db.get_job(job.job_id)["message"]
    assert "look like films, TV or music were held back" in message, message
    assert "seem personal" in message, message


def test_the_estimate_the_dry_run_and_the_real_run_agree(drive):
    root, dest = drive
    a_download_folder(root)
    estimate = ArchiveJob([str(root)], str(dest))
    counted = sorted(name for _root, name in estimate._walk())
    plan = ArchiveJob([str(root)], str(dest), mode=scanner.MODE_DRY_RUN)
    plan.run()
    real = ArchiveJob([str(root)], str(dest))
    real.run()
    def states(job):
        return {(e["category"], e["state"], e["files"], e["spared"]) for e in job.entertainment}

    assert states(estimate) == states(plan) == states(real)
    assert counted == sorted(archived_names(dest))


def test_a_decision_covers_that_kind_only_and_never_the_personal_files(drive):
    root, dest = drive
    folder = a_download_folder(root)
    first = ArchiveJob([str(root)], str(dest))
    list(first._walk())
    keys = {e["category"]: e["key"] for e in first.entertainment}
    db.set_folder_decision(keys["music"], str(folder), "include")
    db.set_folder_decision(keys["film"], str(folder), "exclude")

    job = ArchiveJob([str(root)], str(dest))
    job.run()
    names = archived_names(dest)
    assert sum(1 for n in names if "MassTamilan" in n) == 4, "kept songs arrive, with the spared one"
    assert not any("Kaithi" in n for n in names)
    assert {c: e["state"] for c, e in by_category(job).items()} == {"music": "kept", "film": "excluded"}


def test_a_source_without_audio_is_never_listened_to(drive, monkeypatch):
    root, dest = drive
    a_download_folder(root)
    monkeypatch.setattr(entertainment, "listen", lambda *a, **k: pytest.fail("listened"))
    job = ArchiveJob([{"path": str(root), "types": ["video"]}], str(dest))
    list(job._walk())
    assert set(by_category(job)) == {"film"}


def test_the_status_page_lists_them(drive):
    root, dest = drive
    a_download_folder(root)
    ArchiveJob([str(root)], str(dest)).run()
    page = (dest / status_kit.STATUS_PAGE).read_text(encoding="utf-8")
    assert "music: 3 files held back, 1 personal files copied" in page, page


# ---------------------------------------------------------------------------
# Reviewing them in the console
# ---------------------------------------------------------------------------

from test_archive_console import console as console          # noqa: E402,F401


def test_the_estimate_lists_them_and_a_decision_changes_it(console, tmp_path):
    client, _, _ = console
    root = tmp_path / "Drive"
    folder = a_download_folder(root)
    job = {"source_dirs": [str(root)], "destination_dir": str(tmp_path / "Master")}

    first = client.post("/api/archive/capacity", json=job).get_json()
    assert first["ok"], first
    found = {e["category"]: e for e in first["entertainment"]}
    assert found["music"]["state"] == "held" and found["music"]["files"] == 3
    assert "key" not in found["music"], "the decision key never leaves the server"
    assert first["files"] == 3, "the voice note, the spoken download and the phone video"

    saved = client.post("/api/archive/course-folders/decision", json={"decisions": [
        {"path": str(folder), "decision": "include", "category": "music"}]})
    assert saved.status_code == 200, saved.get_json()
    kept = client.post("/api/archive/capacity", json=job).get_json()
    assert {e["category"]: e["state"] for e in kept["entertainment"]} == {"music": "kept", "film": "held"}
    assert kept["files"] == 6

    bad = client.post("/api/archive/course-folders/decision", json={"decisions": [
        {"path": str(folder), "decision": "exclude", "category": "photos"}]})
    assert bad.status_code == 400
