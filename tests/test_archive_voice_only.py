"""Importing a drive: only the household's own sound, listened to several at a time.

A source drive's survey listened to its songs one at a time, an ffmpeg each
for three excerpts, and the computer sat idle waiting on the drive. These
check that the songs are now heard side by side with the same verdicts, that
with ``import_voice_audio_only`` a plainly named song is held back by how it
sounds while voice notes and anything a phone made still arrive, and that the
"._" files macOS leaves on exFAT drives are not taken for media.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ninaivu.archive import course_material, entertainment, scanner      # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                           # noqa: E402
from test_archive_course_material import archived_names, write           # noqa: E402
from test_archive_course_material import drive as drive                  # noqa: E402,F401
from test_archive_entertainment import (_wav, a_song, music_sound,        # noqa: E402
                                        speech_sound)


@pytest.fixture()
def voice_only(monkeypatch):
    monkeypatch.setattr(scanner, "VOICE_ONLY_AUDIO", True)


def a_plain_folder(root):
    """Songs nobody named as downloads, beside the family's own recordings."""
    folder = root / "Old laptop" / "Sounds"
    _wav(folder / "Wake me up.wav", music_sound(rate=44100), channels=2, rate=44100)
    _wav(folder / "Kannana kanne.wav", music_sound(rate=44100, root=300), channels=2, rate=44100)
    _wav(folder / "Appa talking.wav", speech_sound(rate=44100), channels=2, rate=44100)
    _wav(folder / "New Recording 12.wav", music_sound(rate=44100, root=260), channels=2,
         rate=44100)                                   # a phone's recording of a concert
    _wav(folder / "Patti singing.wav", music_sound(root=180))   # mono, like a voice note
    return folder


# ---------------------------------------------------------------------------
# Voice recordings only
# ---------------------------------------------------------------------------

def test_plain_songs_are_held_back_by_their_sound_and_recordings_arrive(drive, voice_only):
    root, dest = drive
    a_plain_folder(root)

    job = ArchiveJob([str(root)], str(dest))
    job.run()

    assert archived_names(dest) == sorted(
        ["Appa talking.wav", "New Recording 12.wav", "Patti singing.wav"])
    music = next(e for e in job.entertainment if e["category"] == "music")
    assert music["state"] == "held" and music["files"] == 2
    assert any("only voice recordings are imported" in r for r in music["reasons"])


def test_without_the_setting_plain_songs_are_copied_as_before(drive):
    root, dest = drive
    a_plain_folder(root)
    ArchiveJob([str(root)], str(dest)).run()
    assert len(archived_names(dest)) == 5


def test_a_song_that_cannot_be_heard_is_copied(drive, voice_only, monkeypatch):
    root, dest = drive
    a_plain_folder(root)
    monkeypatch.setattr(entertainment, "listen", lambda *a, **k: None)
    ArchiveJob([str(root)], str(dest)).run()
    assert len(archived_names(dest)) == 5, "undecided keeps the file"


def test_a_kept_folder_brings_its_songs_in(drive, voice_only):
    from ninaivu.archive import database as db
    root, dest = drive
    folder = a_plain_folder(root)
    first = ArchiveJob([str(root)], str(dest))
    list(first._walk())
    key = next(e["key"] for e in first.entertainment if e["category"] == "music")
    db.set_folder_decision(key, str(folder), "include")
    ArchiveJob([str(root)], str(dest)).run()
    assert len(archived_names(dest)) == 5


def test_the_estimate_and_the_run_agree(drive, voice_only):
    root, dest = drive
    a_plain_folder(root)
    estimate = ArchiveJob([str(root)], str(dest))
    counted = sorted(name for _root, name in estimate._walk())
    real = ArchiveJob([str(root)], str(dest))
    real.run()
    assert counted == archived_names(dest)


def test_the_setting_is_part_of_what_an_estimate_depends_on(drive, monkeypatch):
    root, dest = drive
    job = ArchiveJob([str(root)], str(dest))
    before = scanner._count_signature(job)
    monkeypatch.setattr(scanner, "VOICE_ONLY_AUDIO", True)
    assert scanner._count_signature(job) != before


def test_the_household_setting_reaches_the_engine(tmp_path, monkeypatch):
    from ninaivu import archive
    from ninaivu.server.config import Config
    assert Config().import_voice_audio_only is True
    monkeypatch.setattr(scanner, "VOICE_ONLY_AUDIO", False)
    archive.configure(tmp_path, None, True)
    assert scanner.VOICE_ONLY_AUDIO is True


# ---------------------------------------------------------------------------
# Several at a time
# ---------------------------------------------------------------------------

def test_a_folders_songs_are_heard_side_by_side_with_the_same_verdicts(drive, monkeypatch):
    root, dest = drive
    folder = root / "Music"
    for i in range(6):
        a_song(folder, f"Track {i}-MassTamilan.com.wav")
    a_song(folder, "Discourse-MassTamilan.com.wav", sound=speech_sound(rate=44100))

    real = entertainment.listen
    lock = threading.Lock()
    at_once = {"now": 0, "most": 0}

    def slow_listen(path, length=None, **kw):
        with lock:
            at_once["now"] += 1
            at_once["most"] = max(at_once["most"], at_once["now"])
        time.sleep(0.2)
        try:
            return real(path, length, **kw)
        finally:
            with lock:
                at_once["now"] -= 1

    monkeypatch.setattr(entertainment, "listen", slow_listen)

    def verdicts(listeners):
        job = ArchiveJob([str(root)], str(dest))
        job.listeners = listeners
        list(job._walk())
        return {(e["category"], e["state"], e["files"], e["spared"]) for e in job.entertainment}

    from ninaivu.archive import database as db
    one = verdicts(1)
    assert at_once["most"] == 1
    db.get_db().execute("DELETE FROM sound_opinions")
    db.get_db().commit()
    at_once["most"] = 0
    several = verdicts(4)
    assert at_once["most"] > 1, "listened to one at a time"
    assert one == several == {("music", "held", 6, 1)}


def test_the_listening_threads_end_with_the_walk(drive):
    root, dest = drive
    a_song(root / "Music", "Track-MassTamilan.com.wav")
    a_song(root / "Music", "Other-MassTamilan.com.wav")
    job = ArchiveJob([str(root)], str(dest))
    list(job._walk())
    assert job._listen_pool is None
    assert not [t for t in threading.enumerate() if t.name.startswith("archive-listen")]


def test_a_stop_while_listening_stops_the_walk(drive, monkeypatch):
    root, dest = drive
    for i in range(4):
        a_song(root / "Music", f"Track {i}-MassTamilan.com.wav")
    job = ArchiveJob([str(root)], str(dest))

    def listen_then_stop(*a, **k):
        job.gate.cancel()
        return ("music", "")

    monkeypatch.setattr(entertainment, "listen", listen_then_stop)
    with pytest.raises(scanner.Cancelled):
        list(job._walk())


def test_each_keeps_the_order_whatever_finishes_first():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as pool:
        out = entertainment._each([0.2, 0.0, 0.1, 0.05],
                                  lambda s: (time.sleep(s), s)[1], pool, lambda: None)
    assert out == [0.2, 0.0, 0.1, 0.05]


@pytest.mark.skipif(not entertainment.FFMPEG, reason="needs ffmpeg")
def test_one_ffmpeg_reads_the_three_excerpts_as_three_would(tmp_path):
    import numpy as np
    song = _wav(tmp_path / "long.wav", music_sound(seconds=120, rate=44100), channels=2,
                rate=44100)
    excerpts = [(20.0, 20.0), (50.0, 20.0), (110.0, 20.0)]       # the last runs past the end
    together = entertainment._decode_excerpts(str(song), excerpts)
    apart = [entertainment._decode(str(song), s, n) for s, n in excerpts]
    assert [len(x) for x in together[:2]] == [len(x) for x in apart[:2]] == [320000, 320000]
    for one, other in zip(together[:2], apart[:2]):
        assert float(np.abs(one - other).max()) < 0.01
    assert 0 < len(apart[2]) < 320000 and len(together[2]) == 320000, "padded to its length"
    past_the_end = entertainment._decode_excerpts(str(song), [(200.0, 20.0), (210.0, 20.0)])
    assert past_the_end is None or [len(x) for x in past_the_end] == [0, 0]
    assert entertainment.listen(str(song), 120)[0] == "music"
    assert entertainment._decode_excerpts(str(tmp_path / "missing.mp3"), excerpts) is None


def test_listening_starts_ffmpeg_once_a_song(tmp_path, monkeypatch):
    import subprocess
    song = _wav(tmp_path / "long.wav", music_sound(seconds=120, rate=44100), channels=2,
                rate=44100)
    if not entertainment.FFMPEG:
        pytest.skip("needs ffmpeg")
    started = []
    real = subprocess.run
    monkeypatch.setattr(entertainment.subprocess, "run",
                        lambda *a, **k: started.append(a) or real(*a, **k))
    assert entertainment.listen(str(song), 120)[0] == "music"
    assert len(started) == 1


# ---------------------------------------------------------------------------
# macOS's "._" files
# ---------------------------------------------------------------------------

def test_apple_double_files_are_not_media(drive):
    root, dest = drive
    folder = root / "DCIM"
    a_song(folder, "Track-MassTamilan.com.wav")
    write(folder / "._Track-MassTamilan.com.wav", b"\0\5\26\7" + b"\0" * 90_000)
    write(folder / "._IMG_0001.MOV", b"\0\5\26\7" + b"\0" * 90_000)
    write(folder / "._IMG_0001.JPG", b"\0\5\26\7" + b"\0" * 90_000)
    job = ArchiveJob([str(root)], str(dest))
    assert [name for _root, name in job._walk()] == []


def test_a_picture_sample_passes_over_apple_double_files(tmp_path):
    write(tmp_path / "._lesson.mp4", b"\0" * 5000)
    write(tmp_path / "lesson.mp4", b"\0" * 5000)
    assert [os.path.basename(p) for p, _ in course_material.sample_pictures(str(tmp_path))] \
        == ["lesson.mp4"]
