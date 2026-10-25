"""Ninaivu's own music under memory films (media/film_music.py): written on
this computer to the film's length, on a beat the cuts can land on, soft, and
played unless somebody chooses otherwise."""

from __future__ import annotations

import shutil
import subprocess
import wave

import numpy as np
import pytest

from ninaivu.media import film_music, films
import test_memory_films as base
from test_memory_films import MONTHS, _photo, _tone, needs_ffmpeg

home = base.home                       # the family app with films switched on

RATE = film_music.RATE


def _loudness(x: np.ndarray, seconds: float = 0.5) -> np.ndarray:
    n = int(seconds * RATE)
    x = x[: x.size // n * n].reshape(-1, n)
    return np.sqrt((x * x).mean(axis=1))


@pytest.mark.parametrize("name", list(film_music.PIECES))
def test_every_piece_is_as_long_as_the_film_soft_and_ends_on_silence(name):
    left, right = film_music.compose(name, 47.3, seed=5)
    assert left.size == right.size == int(47.3 * RATE)
    assert np.isfinite(left).all() and np.isfinite(right).all()
    assert max(np.abs(left).max(), np.abs(right).max()) <= film_music.PEAK + 1e-6
    loud = _loudness(left)
    assert loud.max() <= film_music.LOUDEST_RMS * 2.5
    # Something plays all the way to the last chord, which rings out ...
    assert (loud[2:-int(film_music.LAST_CHORD * 2)] > 0.002).all(), name
    # ... and the film never ends on a cut.
    assert abs(left[-10:]).max() < 1e-3 and abs(right[-10:]).max() < 1e-3


def test_a_seed_changes_the_tune_and_the_same_seed_gives_the_same_music():
    a, _ = film_music.compose("together", 30, seed=1)
    b, _ = film_music.compose("together", 30, seed=1)
    c, _ = film_music.compose("together", 30, seed=2)
    assert np.array_equal(a, b)
    assert not np.allclose(a, c)


def test_the_beats_are_the_pieces_own():
    step = 60 / film_music.PIECES["festival"]["bpm"]
    beats = film_music.beats("festival", 10)
    assert beats[0] == 0 and beats[1] == pytest.approx(step, abs=1e-3)
    assert beats[-1] <= 10 < beats[-1] + step


def test_a_piece_is_chosen_by_the_pace_when_nobody_chose_one():
    assert film_music.piece_for("auto", "gentle") == "morning"
    assert film_music.piece_for("auto", "lively") == "festival"
    assert film_music.piece_for(None, "nonsense") == film_music.DEFAULT
    assert film_music.piece_for("courtyard", "lively") == "courtyard"
    assert {p["name"] for p in film_music.listing()} == set(film_music.PIECES)


def test_the_piece_is_written_as_a_stereo_wav(tmp_path):
    path = film_music.write("courtyard", 14, tmp_path / "m.wav", seed=3)
    with wave.open(str(path)) as got:
        assert (got.getnchannels(), got.getsampwidth(), got.getframerate()) == (2, 2, RATE)
        assert got.getnframes() == 14 * RATE


def test_the_timeline_carries_the_piece_until_it_is_written():
    timeline = films.plan({"items": [{"id": 1, "kind": "picture", "path": "/x/1.jpg",
                                      "width": 4000, "height": 3000, "faces": []}],
                           "music": {"built_in": "morning", "seed": 9}})
    assert timeline["music"] == {"built_in": "morning", "seed": 9}


@needs_ffmpeg
def test_a_film_with_ninaivus_own_music_has_it_ducked_under_the_story(tmp_path):
    items = [_photo(tmp_path / f"p{i}.jpg", i) for i in range(3)]
    _tone(tmp_path / "story.m4a", 4.0, 300)
    items[1]["story"] = {"path": str(tmp_path / "story.m4a"), "language": "en",
                         "segments": [{"start": 0.2, "end": 3.5, "text": "our house"}]}
    info = films.render({"title": "Home", "size": "720p", "items": items, "subtitles": "none",
                         "music": {"built_in": "together", "seed": 4}, "months": MONTHS},
                        tmp_path / "film.mp4", workers=1)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(tmp_path / "film.mp4"), "-vn",
                          "-ac", "1", "-ar", str(RATE), "-f", "f32le", "pipe:1"],
                         capture_output=True, check=True).stdout
    sound = np.frombuffer(pcm, dtype=np.float32)
    assert sound.size / RATE == pytest.approx(info["seconds"], abs=0.3)
    loud = _loudness(sound)
    assert loud[4:-4].min() > 0.002, "music plays under the whole film"
    # The cuts were moved onto the piece's beat.
    timeline = films.plan(films.prepare({"items": items, "title": "Home",
                                         "music": {"built_in": "together"}},
                                        shutil.which("ffprobe")),
                          beats=film_music.beats("together", 120))
    step = film_music.beat_seconds("together")
    cuts = [s["start"] + s["dur"] - timeline["fade"] / 2 for s in timeline["shots"][:-1]]
    on_beat = [c for c in cuts if abs(c / step - round(c / step)) * step < 0.01]
    assert on_beat, cuts


# -- the family app -------------------------------------------------------------------

def test_a_film_gets_ninaivus_own_music_for_its_pace_unless_told_otherwise(home, monkeypatch):
    made = {}
    monkeypatch.setattr(films, "start", lambda film_id, spec, **_: made.update(spec))
    shot = home["ids"]["shot1.jpg"]
    body = {"source": {"kind": "album", "id": home["album"]}, "ids": [shot], "pace": "lively"}
    assert home["family"].post("/api/films", json=body).status_code == 202
    assert made["music"]["built_in"] == "festival"
    seed = made["music"]["seed"]
    assert home["family"].post("/api/films", json=body).status_code == 202
    assert made["music"]["seed"] == seed, "the same moments, the same tune"
    body["music"] = {"kind": "built_in", "name": "courtyard"}
    assert home["family"].post("/api/films", json=body).status_code == 202
    assert made["music"]["built_in"] == "courtyard"
    body["music"] = None
    assert home["family"].post("/api/films", json=body).status_code == 202
    assert made["music"] is None, "no music when asked for none"
    body["music"] = {"kind": "built_in", "name": "../../etc/passwd"}
    assert home["family"].post("/api/films", json=body).status_code == 400

    options = home["family"].get("/api/films/options").get_json()
    assert [p["name"] for p in options["own_music"]] == list(film_music.PIECES)
    assert options["own_music_for"]["gentle"] == "morning"


def test_a_piece_can_be_heard_before_it_is_chosen(home):
    got = home["family"].get("/api/films/music/own/morning")
    assert got.status_code == 200 and got.mimetype == "audio/wav"
    assert got.data[:4] == b"RIFF"
    got.close()
    assert home["family"].get("/api/films/music/own/nothing").status_code == 404
    assert home["guest"].get("/api/films/music/own/morning").status_code in (401, 403)
    home["cfg"].memory_films = False
    assert home["family"].get("/api/films/music/own/morning").status_code == 404
