"""A picture for every sound file, instead of a blank square.

In a grid of pictures, a tile with nothing in it reads as something that
failed to load, or a file that is broken. So a sound file shows its own cover
when it carries one, and otherwise a tile that says what it is: a colour for
its album or folder, a sign for the kind of recording, the shape of the sound,
and its title.

The names below are real ones, from the library this was written for.
"""
from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.media import audio_art, media
from ninaivu.media.audio_art import CALL, MUSIC, SPOKEN, VOICE
from ninaivu.media.scanner import Scanner
from ninaivu.storage import db


def tone(path: Path, seconds: float = 1.5, rate: int = 8000) -> Path:
    """A short WAV: a tone that swells, so its waveform has a shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(
            struct.pack("<h", int(12000 * (i / frames) * math.sin(i / 6)))
            for i in range(frames)))
    return path


def with_cover(path: Path, size=(300, 300)) -> Path:
    """A WAV carrying album art, the way a tagged song does."""
    pytest.importorskip("mutagen")
    from mutagen.id3 import APIC
    from mutagen.wave import WAVE

    art = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(art, "JPEG")
    audio = WAVE(str(path))
    audio.add_tags()
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                        data=art.getvalue()))
    audio.save()
    return path


# --- what kind of recording ---------------------------------------------------

@pytest.mark.parametrize("rel, kind", [
    ("2020/10/10/Call recording Tamildhasan_201010_200915.m4a", CALL),
    ("2016/02/28/. Wife_-919486920540_2016-02-28 12.54.36_O.WAV", CALL),
    ("Calls/2019-03-01 Amma.m4a", CALL),
    ("2006/07/25/08 - Chapter 30, The Pensieve 03.mp3", SPOKEN),
    ("2006/07/25/03 - Ch. 01 - The Other Minister (03).mp3", SPOKEN),
    ("2015/07/27/Edison_Ch16.mp3", SPOKEN),
    ("2007/03/13/HP-OoTP 01 10 - A Peck of Owls (01 of 10).mp3", SPOKEN),
    ("WhatsApp/PTT-20190312-WA0003.opus", VOICE),
    ("Recorder/Voice 012.m4a", VOICE),
    ("2016/10/11/04_SUKI_THALAIMAI_PANBU_03.mp3", MUSIC),
    ("2008/08/09/Marudhaani.mp3", MUSIC),
    # A song about a call is still a song.
    ("music/Call Me Maybe.mp3", MUSIC),
])
def test_the_kind_is_read_from_the_name_and_folder(rel, kind):
    assert audio_art.classify(rel) == kind


def test_a_long_untagged_file_is_talk_and_a_long_album_track_is_not():
    assert audio_art.classify("x/recording.mp3", duration=3600) != MUSIC
    assert audio_art.classify("x/raga.mp3", duration=3600, album="Concert") == MUSIC


# --- what it is called --------------------------------------------------------

@pytest.mark.parametrize("rel, kind, title", [
    ("2016/10/11/04_SUKI_THALAIMAI_PANBU_03.mp3", MUSIC, "Suki Thalaimai Panbu 03"),
    ("2006/12/31/Copy1_THEY DONT CARE.MP3", MUSIC, "They Dont Care"),
    ("2008/06/13/@ENTHINA.MP3", MUSIC, "Enthina"),
    ("2020/10/10/Call recording Tamildhasan_201010_200915.m4a", CALL, "Tamildhasan"),
    ("2016/02/28/. Wife_-919486920540_2016-02-28 12.54.36_O.WAV", CALL, "Wife"),
    ("2007/08/15/Love Song.drm.mp3", MUSIC, "Love Song"),
    # A hash is not a name; the tile falls back to the artist or album.
    ("2021/07/28/28d77ff82ffedb2e3555d35ad3add417.ogg", MUSIC, ""),
])
def test_the_title_is_the_name_tidied_up(rel, kind, title):
    assert audio_art.title_for(rel, kind, None) == title


def test_a_tagged_title_wins_over_the_name():
    assert audio_art.title_for("x/Track 04.mp3", MUSIC, "Marudhaani") == "Marudhaani"


# --- the tile -------------------------------------------------------------------

def test_a_tile_is_a_full_size_picture():
    image = audio_art.tile(MUSIC, title="Marudhaani", seed="Album", levels=[0.5] * 48)
    assert image.size == (audio_art.SIZE, audio_art.SIZE)
    assert image.mode == "RGB"


def test_an_album_is_one_colour_and_different_albums_are_not():
    one = audio_art.colours(MUSIC, "Alaipayuthey")
    assert one == audio_art.colours(MUSIC, "alaipayuthey")
    assert one != audio_art.colours(MUSIC, "Roja")


def test_each_kind_keeps_to_its_own_colours():
    """Calls are all greens, whoever was on the line."""
    def hue(rgb):
        import colorsys
        return colorsys.rgb_to_hls(*(c / 255 for c in rgb))[0] * 360
    for person in ("Amma", "Wife", "Tamildhasan", "Sasisai"):
        assert 120 <= hue(audio_art.colours(CALL, person)[0]) <= 185


def test_letters_the_font_does_not_have_are_not_drawn_as_boxes():
    """Tamil is left off rather than drawn as a row of empty squares."""
    assert not audio_art._drawable("மருதாணி")
    assert audio_art._drawable("Marudhaani")
    # It still makes a tile; there is simply no title on it.
    assert audio_art.tile(MUSIC, title="மருதாணி").size == (audio_art.SIZE,) * 2


# --- the file -------------------------------------------------------------------

def test_a_cover_in_the_file_is_the_picture(tmp_path):
    song = with_cover(tone(tmp_path / "song.wav"), size=(300, 280))
    picture = audio_art.picture_for(song, "song.wav")
    assert picture.size == (300, 280), "the cover, not a drawn tile"


def test_a_file_that_is_not_sound_still_gets_a_tile(tmp_path):
    junk = tmp_path / "broken.mp3"
    junk.write_bytes(b"\x62\x10\x1e\x73" * 500)
    picture = audio_art.picture_for(junk, "2016/broken.mp3")
    assert picture.size == (audio_art.SIZE, audio_art.SIZE)


@pytest.mark.skipif(not media.FFMPEG, reason="needs ffmpeg")
def test_the_waveform_follows_the_sound(tmp_path):
    levels = audio_art.waveform(tone(tmp_path / "swell.wav"))
    assert levels and len(levels) == audio_art.BARS
    assert all(0.0 <= level <= 1.0 for level in levels)
    assert levels[-1] > levels[0], "the tone swells; so must the drawing"


# --- in the library -------------------------------------------------------------

@pytest.fixture()
def sounds(tmp_path):
    from ninaivu.server import auth
    from ninaivu.server.config import Config

    root = tmp_path / "lib"
    tone(root / "2016/10/11/04_SUKI_THALAIMAI_PANBU_03.wav")
    tone(root / "2016/10/11/05_SUKI_THALAIMAI_PANBU_04.wav")
    tone(root / "2020/10/10/Call recording Tamildhasan_201010_200915.wav")
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(root)]
    cfg.active_root = str(root)
    cfg.ai_enabled = False
    cfg.ai_engine = "off"
    cfg.ai_models_dir = str(tmp_path / "ai-models")
    cfg.watch = False
    cfg.workers = 2
    cfg.min_media_bytes = 0
    cfg.ensure_dirs()
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    scanner = Scanner(cfg)
    scanner._run(root, full=True)
    return cfg, conn, scanner


def audio_rows(conn):
    return conn.execute("SELECT * FROM assets WHERE kind='audio' ORDER BY rel_path").fetchall()


def picture_files(cfg, base):
    return [cfg.thumbs_dir / media.thumb_file(base, size, cfg.thumb_format)
            for size in cfg.thumb_sizes]


def test_a_scan_gives_every_sound_file_a_picture(sounds):
    cfg, conn, _ = sounds
    rows = audio_rows(conn)
    assert len(rows) == 3
    for row in rows:
        assert row["thumb"] and row["blurhash"]
        assert all(p.is_file() for p in picture_files(cfg, row["thumb"])), row["rel_path"]


def test_two_tracks_of_one_album_are_not_called_duplicates(sounds):
    """Their tiles look alike on purpose, so they are never fingerprinted."""
    _, conn, _ = sounds
    assert all(row["phash"] is None for row in audio_rows(conn))
    assert all(row["dup_group"] is None for row in audio_rows(conn))


def test_files_indexed_before_they_had_pictures_are_given_them(sounds):
    """The library this was written for had 12,865 sound files and no
    pictures for any of them — nor any reason for a rescan to read them."""
    cfg, conn, scanner = sounds
    row = audio_rows(conn)[0]
    for path in picture_files(cfg, row["thumb"]):
        path.unlink()
    conn.execute("UPDATE assets SET thumb=NULL, indexed_at=1 WHERE id=?", (row["id"],))
    conn.commit()

    scanner._draw_audio_art(conn, cfg.active_root)

    again = conn.execute("SELECT * FROM assets WHERE id=?", (row["id"],)).fetchone()
    assert again["thumb"] == media.thumb_base(cfg.active_root, row["rel_path"])
    assert all(p.is_file() for p in picture_files(cfg, again["thumb"]))
    assert again["indexed_at"] > 1, "the URL must change, or browsers keep the blank"


def test_a_file_on_a_drive_that_is_away_waits_for_it(sounds):
    """Not a lesser tile for good because the drive was unplugged tonight."""
    cfg, conn, scanner = sounds
    row = audio_rows(conn)[0]
    for path in picture_files(cfg, row["thumb"]):
        path.unlink()
    (Path(cfg.active_root) / row["rel_path"]).unlink()

    scanner._draw_audio_art(conn, cfg.active_root)

    assert not any(p.is_file() for p in picture_files(cfg, row["thumb"]))


def test_a_library_with_every_picture_drawn_is_not_read_again(sounds, monkeypatch):
    cfg, conn, scanner = sounds
    monkeypatch.setattr(audio_art, "picture_for",
                        lambda *a, **k: pytest.fail("drew a picture it already had"))
    scanner._draw_audio_art(conn, cfg.active_root)


def test_a_sound_files_picture_is_not_described_as_a_photograph(sounds):
    """Tagged by the image model, a drawn tile would be found by "green" or
    "phone" — its colour and its sign — in a search for photographs."""
    from types import SimpleNamespace

    cfg, conn, scanner = sounds
    seen = []
    scanner.ai = SimpleNamespace(
        name="test", model_id="", semantic=False, device="cpu",
        analyse=lambda paths: seen.extend(paths) or [{} for _ in paths])
    scanner._tag(conn, cfg.active_root)
    assert seen == []


def test_pictures_are_drawn_when_new_files_are_indexed_not_saved_for_the_night(sounds):
    """In the overnight mode analysis waits for 23:00. A picture for a sound
    file is a thumbnail — a third of a second a file — and leaving the tiles
    blank all day for it was the first thing that went wrong on a real library."""
    cfg, conn, scanner = sounds
    asked = []

    class Workload:
        def wait_turn(self, job, stop, on_hold=None):
            asked.append(job)
            return True

    for row in audio_rows(conn):
        for path in picture_files(cfg, row["thumb"]):
            path.unlink()
    scanner.workload = Workload()
    scanner._draw_audio_art(conn, cfg.active_root)
    assert asked and set(asked) == {"index"}
