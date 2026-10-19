"""Memory films: the timeline, the sound, the frames, the MP4, and who may have one.

A film is a copy of photographs, clips and voice stories to take away, so the
rules are the books' (tests/test_occasion_books.py): it never holds a moment
its maker could not have found in the gallery, a story is only told over a
photograph they may open, and a film is its maker's to play and delete.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu import build_services, create_home_app
from ninaivu.media import films
from ninaivu.server import auth
from ninaivu.storage import films as store, stories as story_store

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                  reason="needs ffmpeg")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


def _item(i: int, **extra):
    item = {"id": i, "kind": "picture", "path": f"/x/{i}.jpg", "width": 4000, "height": 3000,
            "rotation": 0, "when": 1_560_000_000 + i * 86400 * 20, "faces": []}
    item.update(extra)
    return item


def _story(seconds: float, *pieces):
    return {"path": "/x/story.m4a", "duration": seconds, "language": "ta",
            "segments": [{"start": a, "end": b, "text": t, "english": e} for a, b, t, e in pieces]}


# -- the timeline ---------------------------------------------------------------

def test_a_film_opens_with_its_title_and_ends_with_a_closing_card():
    timeline = films.plan({"title": "Kodai", "items": [_item(i) for i in range(5)]})
    kinds = [s["kind"] for s in timeline["shots"]]
    assert kinds == ["title", *["picture"] * 5, "end"]
    assert timeline["shots"][0]["lines"][0] == "Kodai"
    # Each shot overlaps the next by one crossfade, and the film ends with the last.
    for a, b in zip(timeline["shots"], timeline["shots"][1:]):
        assert b["start"] == pytest.approx(a["start"] + a["dur"] - films.FADE, abs=0.01)
    last = timeline["shots"][-1]
    assert timeline["duration"] == pytest.approx(last["start"] + last["dur"], abs=0.01)


def test_nothing_to_show_is_an_empty_film():
    assert films.plan({"items": []})["shots"] == []


def test_a_story_is_told_over_its_photograph_with_its_words_underneath():
    told = _story(6.0, (0.5, 3.0, "இது என் அப்பாவின் கடை", "This is my father's shop"),
                  (3.2, 5.8, "1968 ஆம் ஆண்டு", "In 1968"))
    timeline = films.plan({"items": [_item(0), _item(1, story=told)], "subtitles": "both"})
    shot = timeline["shots"][2]
    assert shot["dur"] >= films.STORY_LEAD + 6.0 + films.STORY_TAIL - 0.01
    [voice] = timeline["narration"]
    assert voice["at"] == pytest.approx(shot["start"] + films.STORY_LEAD)
    assert [s["lines"] for s in timeline["subtitles"]] == [
        ["இது என் அப்பாவின் கடை", "This is my father's shop"], ["1968 ஆம் ஆண்டு", "In 1968"]]
    assert timeline["subtitles"][0]["start"] == pytest.approx(voice["at"] + 0.5)
    # The music gives way while somebody speaks.
    assert timeline["duck"] == [[voice["at"], voice["at"] + 6.0]]


@pytest.mark.parametrize("mode, expected", [("spoken", [["சொல்"]]), ("english", [["Said"]]),
                                            ("none", [])])
def test_which_subtitles_is_the_household_s_choice(mode, expected):
    told = _story(2.0, (0.0, 2.0, "சொல்", "Said"))
    timeline = films.plan({"items": [_item(0, story=told)], "subtitles": mode})
    assert [s["lines"] for s in timeline["subtitles"]] == expected


def test_a_long_story_is_cut_at_the_end_of_something_said():
    pieces = [(k * 10.0, k * 10.0 + 8.0, f"piece {k}", "") for k in range(12)]
    timeline = films.plan({"items": [_item(0, story=_story(120.0, *pieces))]})
    [voice] = timeline["narration"]
    assert voice["dur"] == pytest.approx(38.0), "the last piece that ends before 45 s"
    assert max(s["end"] for s in timeline["subtitles"]) <= voice["at"] + voice["dur"] + 0.01


def test_stories_can_be_left_out():
    timeline = films.plan({"items": [_item(0, story=_story(5.0))], "stories": False})
    assert timeline["narration"] == [] and timeline["shots"][1]["dur"] == films.PHOTO_SECONDS


def test_a_clip_gives_its_middle_few_seconds_and_its_own_sound():
    clip = _item(1, kind="video", duration=30.0, has_audio=True)
    timeline = films.plan({"items": [_item(0), clip]})
    shot = timeline["shots"][2]
    assert shot["dur"] == films.CLIP_SECONDS
    assert shot["offset"] == pytest.approx((30.0 - films.CLIP_SECONDS) / 2)
    [sound] = timeline["clip_sound"]
    assert sound["at"] == shot["start"] and sound["offset"] == shot["offset"]
    silent = films.plan({"items": [_item(1, kind="video", duration=3.0)]})
    assert silent["clip_sound"] == [] and silent["shots"][1]["dur"] == pytest.approx(2.8)


def test_cuts_land_on_the_beat_when_one_is_near():
    beats = [k * 0.5 for k in range(400)]                     # 120 bpm
    timeline = films.plan({"items": [_item(i) for i in range(8)]}, beats=beats)
    for shot in timeline["shots"][1:-1]:
        cut = shot["start"] + shot["dur"] - films.FADE / 2
        assert min(abs(cut - b) for b in beats) < 0.01
        assert shot["dur"] >= films.MIN_PHOTO_SECONDS


def test_the_month_is_said_when_it_changes():
    timeline = films.plan({"items": [_item(i) for i in range(4)], "months": MONTHS})
    assert [label["text"] for label in timeline["labels"]][:2] == ["June 2019", "July 2019"]
    one_month = [_item(i, when=1_560_000_000 + i * 60) for i in range(4)]
    assert films.plan({"items": one_month, "months": MONTHS})["labels"] == []


# -- where a photograph moves ----------------------------------------------------

def _inside(inner, outer):
    return (inner[0] >= outer[0] - 1e-6 and inner[1] >= outer[1] - 1e-6
            and inner[0] + inner[2] <= outer[0] + outer[2] + 1e-6
            and inner[1] + inner[3] <= outer[1] + outer[3] + 1e-6)


def test_a_move_closes_in_on_the_faces_and_stays_inside_the_photograph():
    face = [0.70, 0.20, 0.08, 0.10]
    start, end = films.frame_boxes(_item(0, faces=[face]), 16 / 9, 0)
    for box in (start, end):
        assert _inside(box, [0, 0, 1, 1])
        assert box[2] / box[3] == pytest.approx((16 / 9) / (4000 / 3000))
    assert end[2] < start[2], "an even shot closes in"
    assert _inside(face, end), "and ends on the face"
    # An odd shot opens out from the same place instead.
    again = films.frame_boxes(_item(0, faces=[face]), 16 / 9, 1)
    assert again == (end, start)


def test_a_portrait_is_shown_whole_over_a_blur():
    timeline = films.plan({"items": [_item(0, width=3000, height=4000), _item(1)]})
    assert timeline["shots"][1]["backdrop"] and not timeline["shots"][2]["backdrop"]


def test_the_frames_of_a_still_move_smoothly():
    image = Image.new("RGB", (800, 600))
    ImageDraw.Draw(image).rectangle((380, 280, 420, 320), fill=(255, 255, 255))
    shot = {"kind": "picture", "item": 0, "dur": 4.0,
            "from": [0, 0.125, 1, 0.75], "to": [0.1, 0.2, 0.8, 0.6]}
    still = films.Still.__new__(films.Still)
    still.size, still.shot, still.image = (320, 180), shot, image
    still.boxes = (shot["from"], shot["to"])
    centres = []
    for k in range(0, 121, 10):
        frame = still.frame(k / 30)
        xs = [x for x in range(320) if frame.getpixel((x, 90))[0] > 128]
        centres.append((xs[0] + xs[-1]) / 2)
    widths = []
    for k in (0, 120):
        frame = still.frame(k / 30)
        xs = [x for x in range(320) if frame.getpixel((x, 90))[0] > 128]
        widths.append(len(xs))
    assert widths[1] > widths[0], "the square grows as the frame closes in"
    assert all(abs(c - 160) <= 2 for c in centres), "the middle stays the middle"


# -- the beat ---------------------------------------------------------------------

def test_the_beat_of_a_click_track_is_found():
    np = pytest.importorskip("numpy")
    rate = films.BEAT_RATE
    t = np.arange(rate * 20) / rate
    clicks = ((t % 0.5) < 0.02) * np.sin(2 * np.pi * 1500 * t)
    beats = films.beats_of(clicks.astype("float32") + 0.01 * np.random.default_rng(1)
                           .standard_normal(t.size).astype("float32"))
    assert len(beats) > 30
    gaps = np.diff(beats)
    assert np.median(gaps) == pytest.approx(0.5, abs=0.03)
    assert min(abs(beats[5] - k * 0.5) for k in range(40)) < 0.06


def test_silence_has_no_beat():
    np = pytest.importorskip("numpy")
    assert films.beats_of(np.zeros(films.BEAT_RATE * 5, dtype="float32")) == []


# -- the sound ---------------------------------------------------------------------

def test_the_music_is_ducked_smoothly_and_overlapping_voices_once():
    expr = films.duck_expression([[2.0, 5.0], [5.5, 8.0], [20.0, 22.0]])
    assert expr.count("clip(") == 2, "two voices half a second apart are one dip"
    assert films.duck_expression([]) == str(films.MUSIC_LEVEL)


def test_subtitles_are_written_as_subrip():
    text = films.srt([{"start": 1.25, "end": 3.5, "lines": ["சொல்", "Said"]},
                      {"start": 61.0, "end": 62.0, "lines": ["only"]}], 1)
    assert text == "1\n00:00:01,250 --> 00:00:03,500\nSaid\n"


# -- making it side by side ----------------------------------------------------------

def test_the_film_is_cut_into_parts_of_at_least_ten_seconds():
    assert films.parts(3000, 8) == [(0, 375), (375, 750), (750, 1125), (1125, 1500),
                                    (1500, 1875), (1875, 2250), (2250, 2625), (2625, 3000)]
    assert films.parts(400, 8) == [(0, 400)]
    assert films.parts(700, 8) == [(0, 350), (350, 700)]
    assert films.part_count(20) == 8 and films.part_count(4) == 2 and films.part_count(1) == 1


def _photo(path: Path, i: int, size=(1200, 900)) -> dict:
    image = Image.new("RGB", size, (30 + 30 * i, 90, 200 - 20 * i))
    draw = ImageDraw.Draw(image)
    for k in range(12):
        draw.ellipse((k * 90, k * 60, k * 90 + 120, k * 60 + 120), fill=(250, 240 - 15 * k, 20 * k))
    image.save(path, quality=88)
    return _item(i, path=str(path), width=size[0], height=size[1])


def test_any_stretch_of_the_film_comes_out_as_it_would_in_one_go(tmp_path):
    items = [_photo(tmp_path / f"p{i}.jpg", i) for i in range(3)]
    timeline = films.plan({"title": "Part", "items": items, "size": "720p"})
    timeline["size"] = [320, 180]
    whole = list(films.frames(timeline, items, "ffmpeg", 0, 200))
    tail = list(films.frames(timeline, items, "ffmpeg", 150, 200))
    assert len(tail) == 50
    for a, b in zip(whole[150:], tail):
        assert a.tobytes() == b.tobytes()


# -- a real film --------------------------------------------------------------------

def _tone(path: Path, seconds: float, freq: int = 440) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=f={freq}:d={seconds}", str(path)], check=True)


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], capture_output=True, check=True).stdout
    return json.loads(out)


@needs_ffmpeg
def test_a_film_is_an_mp4_with_sound_and_subtitles_in_both_languages(tmp_path):
    items = [_photo(tmp_path / f"p{i}.jpg", i) for i in range(4)]
    items[2]["width"], items[2]["height"] = 900, 1200
    _photo(tmp_path / "p2.jpg", 2, (900, 1200))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=30", "-f", "lavfi", "-i", "sine=f=660",
                    "-t", "6", "-c:v", "libx264", "-c:a", "aac", "-shortest",
                    str(tmp_path / "clip.mp4")], check=True)
    items.insert(2, _item(9, kind="video", path=str(tmp_path / "clip.mp4"), width=320,
                          height=240))
    _tone(tmp_path / "story.m4a", 4.0, 300)
    items[1]["story"] = {"path": str(tmp_path / "story.m4a"), "language": "ta",
                         "segments": [{"start": 0.2, "end": 3.5, "text": "இது எங்கள் வீடு",
                                       "english": "This is our house"}]}
    _tone(tmp_path / "song.m4a", 20.0, 220)
    seen = []
    info = films.render({"title": "எங்கள் வீடு", "subtitle": "2019", "size": "720p",
                         "items": items, "music": {"path": str(tmp_path / "song.m4a")},
                         "months": MONTHS}, tmp_path / "film.mp4",
                        report=lambda done, total: seen.append((done, total)), workers=2)
    probe = _probe(tmp_path / "film.mp4")
    kinds = [s["codec_type"] for s in probe["streams"]]
    assert kinds == ["video", "audio", "subtitle", "subtitle"]
    video = probe["streams"][0]
    assert (video["width"], video["height"]) == (1280, 720)
    assert int(video["nb_frames"]) == info["frames"] == math.ceil(info["seconds"] * films.FPS)
    assert float(probe["format"]["duration"]) == pytest.approx(info["seconds"], abs=0.2)
    assert [s["tags"]["language"] for s in probe["streams"][2:]] == ["tam", "eng"]
    assert seen[-1] == (info["frames"], info["frames"])
    assert info["parts"] == 2


@needs_ffmpeg
def test_a_stopped_film_leaves_no_file(tmp_path):
    items = [_photo(tmp_path / f"p{i}.jpg", i) for i in range(3)]
    with pytest.raises(films.Cancelled):
        films.render({"items": items, "size": "720p"}, tmp_path / "film.mp4",
                     cancelled=lambda: True, workers=1)
    assert not (tmp_path / "film.mp4").exists()


def test_clips_are_about_one_moment_in_seven():
    rows = [{"id": i, "kind": "picture", "captured_at": 1_700_000_000 + i * 600,
             "width": 4000, "height": 3000, "sharpness": 300.0, "quality": [],
             "phash": f"{random.Random(i).getrandbits(64):016x}", "dup_group": None}
            for i in range(80)]
    rows += [{"id": 100 + i, "kind": "video", "captured_at": 1_700_000_000 + i * 6000,
              "duration": 12.0, "width": 1920, "height": 1080, "quality": []} for i in range(10)]
    rows.append({"id": 999, "kind": "video", "captured_at": 1_700_000_000, "duration": 1.0})
    picked = films.pick(rows, 35)
    clips = [r for r in picked if r["kind"] == "video"]
    assert len(picked) == 35 and len(clips) == 5
    assert 999 not in {r["id"] for r in clips}, "a clip of a second is a mistake"
    times = [r["captured_at"] for r in picked]
    assert times == sorted(times)


# -- the family app -------------------------------------------------------------------

@pytest.fixture()
def home(scanned, monkeypatch):
    cfg, conn, scanner = scanned
    cfg.watch = False
    cfg.memory_films = True
    films.reset()
    root = Path(cfg.active_root)
    if shutil.which("ffmpeg"):
        (root / "music").mkdir()
        _tone(root / "music" / "Veena morning.m4a", 12.0, 330)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=320x240:rate=30", "-t", "4", "-c:v", "libx264",
                        str(root / "2023/05/12/clip.mp4")], check=True)
        # A Live Photo: a still with its moving part of the same name beside it.
        live = next(root.glob("*/*/*/shot3.jpg"))
        shutil.copy(root / "2023/05/12/clip.mp4", live.with_suffix(".mov"))
        stamp = live.stat().st_mtime + 5
        __import__("os").utime(live, (stamp, stamp))           # looked at again
        from ninaivu.media import scanner as scanner_mod
        scanner_mod._clips_beside.cache_clear()
        scanner._run(root, full=True)
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                     role=auth.ROLE_GUEST, created_by=admin.id)
    auth.create_user(conn, "cousin", "cousin-password-1", display_name="Cousin",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    conn.execute("UPDATE assets SET visibility=2 WHERE folder='private'")
    for (asset_id,) in conn.execute("SELECT id FROM assets").fetchall():
        conn.execute("UPDATE assets SET phash=?, dup_group=NULL, quality='[]' WHERE id=?",
                     (f"{random.Random(asset_id).getrandbits(64):016x}", asset_id))
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}
    conn.commit()
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    clients = {"family": login(app.test_client(), *FAMILY),
               "admin": login(app.test_client(), *ADMIN),
               "guest": login(app.test_client(), *GUEST),
               "cousin": login(app.test_client(), "cousin", "cousin-password-1")}
    album = clients["admin"].post("/api/albums", json={
        "name": "Everything", "ids": sorted(ids.values())}).get_json()["id"]
    yield {"ids": ids, "conn": conn, "cfg": cfg, "album": album, **clients}
    films.reset()
    services.stop(timeout=5.0)


def _pick(client, album, count=12):
    return client.post("/api/films/pick", json={"source": {"kind": "album", "id": album},
                                                 "count": count})


def test_switched_off_there_are_no_films(home):
    home["cfg"].memory_films = False
    assert home["family"].get("/api/films").status_code == 404
    assert home["family"].get("/api/films/options").status_code == 404
    assert _pick(home["family"], home["album"]).status_code == 404


def test_a_family_member_never_gets_admin_only_photographs(home):
    secret = {home["ids"][f"secret{i}.jpg"] for i in range(3)}
    family = _pick(home["family"], home["album"], 60)
    assert family.status_code == 200
    assert {i["id"] for i in family.get_json()["items"]}.isdisjoint(secret)
    assert _pick(home["guest"], home["album"]).status_code in (401, 403)
    assert _pick(home["family"], home["album"], 3).status_code == 400


def test_making_cannot_widen_what_a_pick_showed(home, monkeypatch):
    made = {}
    monkeypatch.setattr(films, "start", lambda film_id, spec, **_: made.update(spec))
    secret, shot = home["ids"]["secret0.jpg"], home["ids"]["shot1.jpg"]
    response = home["family"].post("/api/films", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": [shot, secret]})
    assert response.status_code == 202
    assert [i["id"] for i in made["items"]] == [shot]


def test_a_story_is_told_with_its_words_and_a_song_must_be_one_the_viewer_can_find(home, monkeypatch):
    made = {}
    monkeypatch.setattr(films, "start", lambda film_id, spec, **_: made.update(spec))
    conn, shot = home["conn"], home["ids"]["shot2.jpg"]
    folder = story_store.folder_for(home["cfg"].state_dir)
    told = story_store.add(conn, folder, shot, b"RIFF0000WAVE", mime="audio/wav",
                           created_by=None, duration=3.0)
    conn.execute("INSERT INTO story_words(story_id, language, words, english, segments, made_at) "
                 "VALUES(?, 'ta', 'வீடு', 'House', ?, 0)",
                 (told["id"], json.dumps([{"start": 0, "end": 2, "text": "வீடு",
                                           "english": "House"}])))
    conn.commit()
    body = {"source": {"kind": "album", "id": home["album"]}, "ids": [shot]}
    assert home["family"].post("/api/films", json=body).status_code == 202
    story = made["items"][0]["story"]
    assert story["language"] == "ta" and story["segments"][0]["english"] == "House"
    assert Path(story["path"]).parent == folder.resolve()
    maya = conn.execute("SELECT id FROM users WHERE username=?", (FAMILY[0],)).fetchone()[0]
    film = store.listed(conn, maya)[0]
    assert film["story_ids"] == [told["id"]]
    made.clear()
    body["stories"] = False
    assert home["family"].post("/api/films", json=body).status_code == 202
    assert "story" not in made["items"][0]
    body["music"] = {"kind": "library", "id": home["ids"]["shot1.jpg"]}
    assert home["family"].post("/api/films", json=body).status_code == 400, "not a song"
    body["music"] = {"kind": "upload", "token": "0" * 32}
    assert home["family"].post("/api/films", json=body).status_code == 400, "nobody brought it"


@needs_ffmpeg
def test_a_film_is_made_played_by_its_owner_and_deleted(home):
    # Sound files in the library are the administrators' alone ...
    song = home["ids"]["Veena morning.m4a"]
    assert [s["id"] for s in home["admin"].get("/api/films/options").get_json()["music"]] == [song]
    assert home["family"].get("/api/films/options").get_json()["music"] == []
    # ... so a family member brings their own, which is theirs alone.
    library = Path(home["cfg"].active_root)
    brought = home["family"].post("/api/films/music", data={
        "audio": ((library / "music" / "Veena morning.m4a").open("rb"), "Kalyani raga.m4a")},
        content_type="multipart/form-data")
    assert brought.status_code == 201, brought.get_json()
    token = brought.get_json()["token"]
    options = home["family"].get("/api/films/options").get_json()
    assert [(s["token"], s["title"]) for s in options["my_music"]] == [(token, "Kalyani raga")]
    assert home["cousin"].get("/api/films/options").get_json()["my_music"] == []
    assert home["family"].post("/api/films/music", data={
        "audio": (__import__("io").BytesIO(b"<html>not a song</html>"), "x.mp3")},
        content_type="multipart/form-data").status_code == 400
    picked = _pick(home["family"], home["album"], 8).get_json()["items"]
    assert "video" in {i["kind"] for i in picked}
    made = home["family"].post("/api/films", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": [i["id"] for i in picked],
        "size": "720p", "title": "எங்கள் 2023", "music": {"kind": "upload", "token": token},
        "months": MONTHS})
    assert made.status_code == 202, made.get_json()
    film_id = made.get_json()["id"]
    assert films.wait(film_id, 300) == "done", store.get(home["conn"], film_id)["error"]
    status = home["family"].get(f"/api/films/{film_id}").get_json()
    assert status["state"] == "done" and status["seconds"] > 10, status
    assert [f["id"] for f in home["family"].get("/api/films").get_json()["films"]] == [film_id]
    assert home["cousin"].get("/api/films").get_json()["films"] == []

    got = home["family"].get(status["play"], headers={"Range": "bytes=0-99"})
    assert got.status_code == 206 and got.mimetype == "video/mp4" and len(got.data) == 100
    assert got.data[4:8] == b"ftyp"
    got.close()
    download = home["family"].get(status["download"])
    assert download.status_code == 200 and "attachment" in download.headers["Content-Disposition"]
    download.close()
    assert home["cousin"].get(status["play"]).status_code == 404, "somebody else's film"
    assert home["guest"].get(status["play"]).status_code in (401, 403)

    path = store.file_of(home["cfg"].state_dir, store.get(home["conn"], film_id))
    assert path.is_file() and path.parent.name == "films"
    # A photograph in it hidden since: the film is not handed out again.
    hidden = store.get(home["conn"], film_id)["item_ids"][0]
    home["conn"].execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden,))
    home["conn"].commit()
    assert home["family"].get(status["play"]).status_code == 410
    assert home["family"].delete(f"/api/films/{film_id}").status_code == 200
    assert not path.exists()
    assert home["family"].get(f"/api/films/{film_id}").status_code == 404
    assert home["cousin"].delete(f"/api/films/music/{token}").status_code == 404
    assert home["family"].delete(f"/api/films/music/{token}").status_code == 200
    assert home["family"].get("/api/films/options").get_json()["my_music"] == []


def test_one_film_at_a_time(home, monkeypatch):
    monkeypatch.setattr(films, "building", lambda: True)
    response = home["family"].post("/api/films", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": [home["ids"]["shot1.jpg"]]})
    assert response.status_code == 409
    assert home["family"].get("/api/films").get_json()["films"] == []


def test_a_film_of_clips_alone_has_its_title_on_a_plain_ground():
    clips = [_item(i, kind="video", duration=8.0) for i in range(3)]
    timeline = films.plan({"title": "Clips", "items": clips})
    assert timeline["shots"][0]["item"] is None and timeline["shots"][-1]["item"] is None


@pytest.mark.parametrize("pace", ["gentle", "lively"])
def test_the_pace_sets_how_long_a_photograph_stays(pace):
    seconds, fade = films.PACES[pace]
    timeline = films.plan({"items": [_item(i) for i in range(4)], "pace": pace})
    assert timeline["fade"] == fade
    assert [s["dur"] for s in timeline["shots"][1:-1]] == [seconds] * 4


def test_a_look_colours_the_whole_frame():
    frame = Image.new("RGB", (8, 8), (200, 120, 40))
    r, g, b = films.look(frame, "mono").getpixel((0, 0))
    assert r == g == b
    warm = films.look(frame, "warm").getpixel((0, 0))
    assert warm[0] > 200 and warm[2] < 40
    assert films.look(frame, "natural") is frame


def test_a_live_photo_moves_for_its_few_seconds():
    live = _item(0, kind="live", duration=2.9)
    timeline = films.plan({"items": [_item(1), live]})
    assert timeline["shots"][2]["kind"] == "live"
    assert timeline["shots"][2]["dur"] == pytest.approx(2.8)
    assert timeline["shots"][0]["item"] == 0, "the title is over a photograph, not the Live clip"


@needs_ffmpeg
def test_a_live_photo_is_shown_moving_unless_a_story_is_told_over_it(home, monkeypatch):
    made = {}
    monkeypatch.setattr(films, "start", lambda film_id, spec, **_: made.update(spec))
    shot = home["ids"]["shot3.jpg"]
    assert home["conn"].execute("SELECT is_live FROM assets WHERE id=?", (shot,)).fetchone()[0]
    body = {"source": {"kind": "album", "id": home["album"]}, "ids": [shot], "pace": "lively",
            "look": "warm"}
    assert home["family"].post("/api/films", json=body).status_code == 202
    [item] = made["items"]
    assert item["kind"] == "live" and item["path"].endswith("shot3.mov")
    assert item["still"].endswith("shot3.jpg")
    assert (made["pace"], made["look"]) == ("lively", "warm")
    body["live"] = False
    assert home["family"].post("/api/films", json=body).status_code == 202
    assert made["items"][0]["kind"] == "picture"
    body["look"] = "sepia"
    assert home["family"].post("/api/films", json=body).status_code == 400


# -- growing up, then and now, chapters (plans E and F) ---------------------------

YEAR = 365.25 * 86400


def test_eyes_are_put_level_in_the_same_place_whatever_the_photograph():
    for eyes in (((100.0, 300.0), (180.0, 330.0)), ((900.0, 120.0), (1300.0, 60.0))):
        a, b, c, d, e, f = films.align_matrix(eyes, (640.0, 300.0), 140.0)
        # The inverse map: where in the photograph each frame pixel is read.
        # The targets must read back the eyes.
        for target, eye in zip(((570.0, 300.0), (710.0, 300.0)), eyes):
            x, y = target
            assert math.isclose(a * x + b * y + c, eye[0], abs_tol=0.01)
            assert math.isclose(d * x + e * y + f, eye[1], abs_tol=0.01)


def test_a_head_lying_down_is_placed_but_not_turned():
    a, b, _, d, _, _ = films.align_matrix(((100.0, 100.0), (100.0, 200.0)), (0.0, 0.0), 50.0)
    assert b == 0 and d == 0 and a > 0


def _held_rows(n: int, start_year: int = 1990, step: float = 1.0):
    rows, faces = [], {}
    for i in range(n):
        rows.append({"id": i + 1, "kind": "picture", "captured_at": (start_year - 1970 + i * step) * YEAR,
                     "width": 2000, "height": 1500})
        faces[i + 1] = {"size": 0.1 + (i % 3) * 0.05, "quality": 0.8,
                        "eyes": [[0.4, 0.4], [0.5, 0.4]]}
    return rows, faces


def test_growing_up_shares_the_years_out_and_holds_only_clear_faces():
    rows, faces = _held_rows(30, step=0.2)                  # six years, five a year
    faces[1]["size"] = 0.01                                 # too small to hold
    del faces[2]["eyes"]                                    # no eyes found
    picked = films.pick_growing_up(rows, faces, count=12)
    years = [__import__("time").gmtime(r["when"]).tm_year for r in picked]
    assert len(picked) == 12 and years == sorted(years)
    assert {1, 2}.isdisjoint(r["id"] for r in picked)
    assert max(years.count(y) for y in set(years)) - min(years.count(y) for y in set(years)) <= 1


def test_then_and_now_pairs_are_years_apart_and_need_the_years():
    rows, faces = _held_rows(20, step=1.5)                  # thirty years
    pairs = films.pick_then_and_now(rows, faces, pairs=4)
    assert 1 <= len(pairs) <= 4
    for then, now in pairs:
        assert now["when"] - then["when"] >= films.PAIR_YEARS * YEAR
    assert len({r["id"] for pair in pairs for r in pair}) == 2 * len(pairs)
    short, short_faces = _held_rows(8, step=0.3)            # two years only
    assert films.pick_then_and_now(short, short_faces) == []


def test_a_growing_up_film_is_quick_holds_the_eyes_and_writes_the_years():
    items = [_item(i, eyes=[[0.4, 0.4], [0.5, 0.41]], label=str(2000 + i), story=_story(9.0))
             for i in range(5)]
    timeline = films.plan({"title": "Arun", "style": "growing_up", "items": items, "months": MONTHS})
    photos = [s for s in timeline["shots"] if s["kind"] == "picture"]
    assert all(s.get("align") and s["dur"] == films.GROWING_PACES["steady"][0] for s in photos)
    assert timeline["narration"] == [] and timeline["subtitles"] == []   # too quick for stories
    assert [lb["text"] for lb in timeline["labels"]] == [str(2000 + i) for i in range(5)]


def test_a_pair_shows_both_years_under_their_halves():
    then = _item(1, eyes=[[0.4, 0.4], [0.5, 0.4]], label="1985")
    now = _item(2, eyes=[[0.4, 0.4], [0.5, 0.4]], label="2024")
    timeline = films.plan({"title": "Amma", "style": "then_and_now",
                           "items": [{"id": 1, "kind": "pair", "then": then, "now": now}]})
    (pair,) = [s for s in timeline["shots"] if s["kind"] == "pair"]
    assert pair["dur"] == films.PAIR_PACES["steady"][0]
    assert [(lb["text"], lb["at"][0]) for lb in timeline["labels"]] == [("1985", 0.25), ("2024", 0.75)]


def test_chapters_get_a_heading_before_their_first_moment_and_no_months():
    items = [_item(i) for i in range(8)]
    timeline = films.plan({"title": "Trip", "items": items, "months": MONTHS,
                           "chapters": [{"at": 0, "title": "Madurai", "line": "The temple first."},
                                        {"at": 5, "title": "Kodai", "line": ""},
                                        {"at": 99, "title": "Nowhere"}]})
    kinds = [(s["kind"], s.get("item")) for s in timeline["shots"]]
    assert kinds[1] == ("chapter", 0) and kinds[2] == ("picture", 0)
    chapter = [s for s in timeline["shots"] if s["kind"] == "chapter"]
    assert [c["lines"] for c in chapter] == [["Madurai", "The temple first."], ["Kodai", ""]]
    assert timeline["labels"] == []


@needs_ffmpeg
def test_growing_up_and_then_and_now_films_are_made(tmp_path):
    def face(i, size):
        image = Image.new("RGB", size, (40 + 40 * i, 110, 90))
        draw = ImageDraw.Draw(image)
        cx, cy, d = size[0] * (0.3 + 0.2 * i), size[1] * 0.45, 60 + 40 * i
        draw.ellipse((cx - d * 1.2, cy - d * 1.2, cx + d * 1.2, cy + d * 1.6), fill=(220, 190, 160))
        for x in (cx - d / 2, cx + d / 2):
            draw.ellipse((x - 6, cy - 6, x + 6, cy + 6), fill=(255, 0, 0))
        path = tmp_path / f"f{i}.jpg"
        image.save(path)
        return _item(i, path=str(path), width=size[0], height=size[1], label=str(1990 + 10 * i),
                     eyes=[[(cx - d / 2) / size[0], cy / size[1]], [(cx + d / 2) / size[0], cy / size[1]]])

    held = [face(i, (1600, 1200) if i % 2 else (1200, 1600)) for i in range(3)]
    timeline = films.plan({"title": "Arun", "style": "growing_up", "items": held, "size": "720p"})
    frame = next(f for k, f in enumerate(films.frames(timeline, held, "ffmpeg"))
                 if k == int((timeline["shots"][2]["start"] + 0.9) * films.FPS))
    red = [(x, y) for x in range(0, 1280, 2) for y in range(0, 720, 2)
           if frame.getpixel((x, y))[0] > 200 and frame.getpixel((x, y))[1] < 80]
    xs, ys = [p[0] for p in red], [p[1] for p in red]
    # The eyes sit either side of the middle, level, where the film keeps them.
    assert abs((min(xs) + max(xs)) / 2 - 640) < 12 and abs((min(ys) + max(ys)) / 2 - 302) < 12
    out = tmp_path / "pair.mp4"
    films.render({"title": "Then and now", "style": "then_and_now", "size": "720p",
                  "items": [{"id": 0, "kind": "pair", "then": held[0], "now": held[2]}]},
                 out, report=lambda d, t: None, cancelled=lambda: False, workers=1)
    assert out.stat().st_size > 10000


def test_a_storyboard_from_dates_and_places_turns_where_the_film_does():
    from ninaivu.media import storyboard
    day = 86400
    moments = ([{"id": i, "when": 1_600_000_000 + i * 3600, "place": "Madurai"} for i in range(5)]
               + [{"id": 10 + i, "when": 1_600_000_000 + 10 * day + i * 3600, "place": "Kodaikanal"}
                  for i in range(4)]
               + [{"id": 20, "when": 1_600_000_000 + 30 * day, "place": ""}])
    board = storyboard.by_rules(moments, title="Summer", months=MONTHS)
    assert board["by"] == "rules" and board["title"] == "Summer"
    assert [(c["id"], c["title"]) for c in board["chapters"]] == [(0, "Madurai"), (10, "Kodaikanal")]
    one_day = storyboard.by_rules(moments[:5], months=MONTHS)
    assert one_day["chapters"] == [] and one_day["title"] == "Madurai"


def test_the_writing_model_s_storyboard_is_checked_before_it_is_used():
    from ninaivu.media import storyboard
    from ninaivu.runtime import HostCrashed
    moments = [{"id": 100 + i, "when": 1_600_000_000 + i * 86400 * 5, "place": "", "people": ["Amma"],
                "about": "A family at a temple.", "story": "அன்று மழை பெய்தது"} for i in range(9)]

    class Host:
        def __init__(self, answer):
            self.answer, self.asked = answer, []

        def call(self, name, **kwargs):
            self.asked.append((name, kwargs))
            if isinstance(self.answer, Exception):
                raise self.answer
            return {"text": json.dumps(self.answer, ensure_ascii=False), "model": "text-model"}

    good = {"title": "மதுரைப் பயணம்", "subtitle": "2020", "closing": "மீண்டும் போவோம்",
            "chapters": [{"starts_at": 0, "title": "கோயில்", "line": "முதல் நாள்."},
                         {"starts_at": 4, "title": "மழை", "line": ""}]}
    host = Host(good)
    board = storyboard.draft(moments, language="ta", months=MONTHS, host=host)
    assert board["by"] == "model" and board["model"] == "text-model"
    assert [(c["id"], c["title"]) for c in board["chapters"]] == [(100, "கோயில்"), (104, "மழை")]
    name, asked = host.asked[0]
    assert name == "generate" and asked["json_schema"] == storyboard.SCHEMA
    prompt = asked["messages"][1]["content"]
    assert "Amma" in prompt and "அன்று மழை பெய்தது" in prompt and "Tamil" in asked["messages"][0]["content"]
    for bad in ({**good, "chapters": [{"starts_at": 4, "title": "x", "line": ""},
                                      {"starts_at": 2, "title": "y", "line": ""}]},
                {**good, "chapters": [{"starts_at": 0, "title": "x", "line": ""},
                                      {"starts_at": 99, "title": "y", "line": ""}]},
                {**good, "title": ""}, HostCrashed("gone")):
        assert storyboard.draft(moments, language="ta", months=MONTHS, host=Host(bad))["by"] == "rules"


def _person(home, years=(1992, 1996, 2001, 2008, 2015, 2023)):
    """A named person in every photograph of the library, across the years."""
    conn = home["conn"]
    cur = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Arun', 0)")
    person = cur.lastrowid
    rows = conn.execute("SELECT id, width, height FROM assets WHERE kind='picture' "
                        "AND folder != 'private' ORDER BY id").fetchall()
    for n, (asset_id, w, h) in enumerate(rows):
        year = years[n % len(years)]
        conn.execute("UPDATE assets SET captured_at=? WHERE id=?",
                     ((year - 1970) * YEAR + n * 86400, asset_id))
        x, y, size = w * 0.35, h * 0.3, w * 0.2
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, landmarks, quality, "
                     "embedding, created_at) VALUES (?, ?, 'confirmed', ?, ?, 0.9, ?, 0)",
                     (asset_id, person, json.dumps([x, y, size, size]),
                      json.dumps([[x + size * 0.3, y + size * 0.4], [x + size * 0.7, y + size * 0.4],
                                  [x + size / 2, y + size * 0.6], [x + size * 0.35, y + size * 0.8],
                                  [x + size * 0.65, y + size * 0.8]]), b"\0" * 8))
    conn.commit()
    return person


def test_growing_up_and_then_and_now_are_for_a_person(home):
    client = home["family"]
    for style in ("growing_up", "then_and_now"):
        got = client.post("/api/films/pick", json={"source": {"kind": "album", "id": home["album"]},
                                                   "style": style})
        assert got.status_code == 400
    assert client.post("/api/films/pick", json={"source": {"kind": "album", "id": home["album"]},
                                                "style": "slideshow"}).status_code == 400


@needs_ffmpeg
def test_a_growing_up_film_of_a_person_is_made_with_their_ages(home):
    person = _person(home)
    client = home["family"]
    source = {"kind": "person", "id": person}
    picked = client.post("/api/films/pick", json={"source": source, "style": "growing_up",
                                                  "count": 6}).get_json()
    ids = [i["id"] for i in picked["items"]]
    assert 1 <= len(ids) <= 6
    private = {r[0] for r in home["conn"].execute("SELECT id FROM assets WHERE folder='private'")}
    assert private.isdisjoint(ids)
    made = client.post("/api/films", json={"source": source, "ids": ids, "style": "growing_up",
                                           "size": "720p", "born": 1990, "age_label": "வயது {age}"})
    assert made.status_code == 202, made.get_json()
    assert films.wait(made.get_json()["id"]) == "done"
    assert client.post("/api/films", json={"source": source, "ids": ids, "style": "growing_up",
                                           "age_label": "no age"}).status_code == 400

    pairs = client.post("/api/films/pick", json={"source": source, "style": "then_and_now"}).get_json()
    assert pairs["pairs"], pairs
    flat = [m["id"] for pair in pairs["pairs"] for m in pair]
    assert client.post("/api/films", json={"source": source, "ids": flat[:-1],
                                           "style": "then_and_now"}).status_code == 400
    made = client.post("/api/films", json={"source": source, "ids": flat, "style": "then_and_now",
                                           "size": "720p"})
    assert made.status_code == 202, made.get_json()
    film_id = made.get_json()["id"]
    assert films.wait(film_id) == "done"
    assert client.get(f"/api/films/{film_id}").get_json()["moments"] == len(flat)


def test_a_storyboard_is_drafted_from_what_the_viewer_may_see(home):
    client = home["family"]
    ids = sorted(home["ids"].values())
    board = client.post("/api/films/storyboard", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": ids, "language": "en",
        "months": MONTHS}).get_json()
    assert board["by"] == "rules" and board["title"] == "Everything"
    private = {r[0] for r in home["conn"].execute("SELECT id FROM assets WHERE folder='private'")}
    assert private.isdisjoint(c["id"] for c in board["chapters"])
    options = client.get("/api/films/options").get_json()
    assert options["writer"] is False and "growing_up" in options["styles"]
    made = client.post("/api/films", json={"source": {"kind": "album", "id": home["album"]},
                                           "ids": ids[:6], "chapters": [{"id": "x", "title": "y"}]})
    assert made.status_code == 400
