"""Playing what a browser cannot: converted, and played while it is converted.

Three promises: a video or sound file in a format a browser will not play is
converted and played; it starts at once, from a live conversion, instead of
after the whole copy is made; and nothing of it is ever written into the
library — the copies are a cache in Ninaivu's own folder.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu import build_services, create_home_app
from ninaivu.server import auth
from ninaivu.utils import proxies

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def _make(path: Path, *args: str) -> Path:
    subprocess.run([FFMPEG, "-v", "error", "-y", *args, str(path)], check=True, timeout=60)
    return path


def _files(folder: Path) -> dict[str, int]:
    return {str(p.relative_to(folder)): p.stat().st_mtime_ns for p in folder.rglob("*") if p.is_file()}


def test_sound_a_browser_will_not_play_needs_a_copy_too():
    assert proxies.needs_proxy("wma", "audio") and proxies.needs_proxy("aiff", "audio")
    assert not proxies.needs_proxy("mp3", "audio") and not proxies.needs_proxy("m4a", "audio")
    assert proxies.needs_proxy("mkv", "video") and not proxies.needs_proxy("jpg", "picture")


@needs_ffmpeg
def test_a_live_conversion_is_a_playable_stream_from_the_first_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: FFMPEG)
    clip = _make(tmp_path / "clip.mkv", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
                 "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "mpeg4", "-c:a", "mp3")
    stream = proxies.live(clip, "video")
    data = b"".join(stream)
    assert data[4:8] == b"ftyp" and b"moof" in data, "fragmented MP4, playable as it arrives"
    # The slot is given back when it ends.
    assert proxies._live_slots._value == proxies.MAX_LIVE          # noqa: SLF001


@needs_ffmpeg
def test_a_live_conversion_can_be_fed_rather_than_read_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: FFMPEG)
    clip = _make(tmp_path / "clip.mkv", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
                 "-c:v", "mpeg4")
    body = clip.read_bytes()
    feed = (body[i:i + 4096] for i in range(0, len(body), 4096))
    data = b"".join(proxies.live(None, "video", feed=feed))
    assert data[4:8] == b"ftyp"


@needs_ffmpeg
def test_leaving_part_way_stops_the_encode_and_frees_its_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: FFMPEG)
    clip = _make(tmp_path / "long.mkv", "-f", "lavfi", "-i", "testsrc=duration=30:size=320x240:rate=25",
                 "-c:v", "mpeg4")
    stream = proxies.live(clip, "video")
    next(stream)
    stream.close()                               # the browser went away
    assert proxies._live_slots._value == proxies.MAX_LIVE          # noqa: SLF001


@needs_ffmpeg
def test_when_every_slot_is_taken_it_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: FFMPEG)
    monkeypatch.setattr(proxies, "_live_slots", __import__("threading").BoundedSemaphore(1))
    clip = _make(tmp_path / "clip.mkv", "-f", "lavfi", "-i", "testsrc=duration=5:size=160x120:rate=10",
                 "-c:v", "mpeg4")
    first = proxies.live(clip, "video")
    with pytest.raises(proxies.Busy):
        proxies.live(clip, "video")
    first.close()


def test_something_ffmpeg_cannot_read_is_an_error_before_any_answer(tmp_path, monkeypatch):
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed")
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: FFMPEG)
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"this is not a video at all" * 100)
    with pytest.raises(OSError):
        proxies.live(junk, "video")
    assert proxies._live_slots._value == proxies.MAX_LIVE          # noqa: SLF001


# --- in the family app --------------------------------------------------------

@pytest.fixture()
def home(scanned, monkeypatch):
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed")
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: FFMPEG)
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                     role=auth.ROLE_GUEST, created_by=admin.id)
    library = Path(cfg.active_root)
    _make(library / "misc" / "camcorder.mkv", "-f", "lavfi", "-i",
          "testsrc=duration=2:size=320x240:rate=10", "-c:v", "mpeg4")
    _make(library / "misc" / "voicemail.wma", "-f", "lavfi", "-i", "sine=duration=2",
          "-c:a", "wmav2")
    ids = {}
    for name, kind in (("camcorder.mkv", "video"), ("voicemail.wma", "audio")):
        ids[name] = conn.execute(
            "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, duration, "
            "visibility) VALUES(?,?,?,?,?,?,?,?,1)",
            (str(library), f"misc/{name}", name, "misc", name.rsplit(".", 1)[1], kind,
             (library / "misc" / name).stat().st_size, 2.0)).lastrowid
    conn.commit()
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    return {"app": app, "cfg": cfg, "ids": ids, "library": library,
            "family": login(app.test_client(), *FAMILY),
            # Sound is administrator-only in Ninaivu (call recordings, voicemail).
            "admin": login(app.test_client(), *ADMIN),
            "guest": login(app.test_client(), *GUEST)}


def test_a_video_plays_at_once_from_a_live_conversion(home):
    answer = home["family"].get(f"/api/stream/{home['ids']['camcorder.mkv']}")
    assert answer.status_code == 200 and answer.mimetype == "video/mp4"
    assert answer.data[4:8] == b"ftyp"
    assert answer.headers["Cache-Control"] == "no-store"


def test_sound_plays_through_a_conversion_too(home):
    live = home["admin"].get(f"/api/stream/{home['ids']['voicemail.wma']}")
    assert live.status_code == 200 and live.mimetype == "audio/mp4" and live.data[4:8] == b"ftyp"
    url = f"/api/proxy/{home['ids']['voicemail.wma']}"
    for _ in range(200):
        answer = home["admin"].get(url, headers={"Range": "bytes=0-0"})
        if answer.status_code != 202:
            break
        time.sleep(0.05)
    assert answer.status_code == 206 and answer.mimetype == "audio/mp4"
    assert home["admin"].get(url).data[4:8] == b"ftyp"


def test_asking_for_the_stream_starts_the_finished_copy(home):
    home["family"].get(f"/api/stream/{home['ids']['camcorder.mkv']}")
    url = f"/api/proxy/{home['ids']['camcorder.mkv']}"
    for _ in range(300):
        answer = home["family"].get(url, headers={"Range": "bytes=0-0"})
        if answer.status_code != 202:
            break
        time.sleep(0.05)
    assert answer.status_code == 206 and answer.headers["Content-Range"].startswith("bytes 0-0/")


def test_the_copies_are_a_cache_and_never_in_the_library(home):
    before = _files(home["library"])
    home["family"].get(f"/api/stream/{home['ids']['camcorder.mkv']}")
    url = f"/api/proxy/{home['ids']['voicemail.wma']}"
    for _ in range(200):
        if home["admin"].get(url, headers={"Range": "bytes=0-0"}).status_code != 202:
            break
        time.sleep(0.05)
    assert _files(home["library"]) == before, "nothing added to or changed in the library"
    made = list(proxies.proxies_dir(home["cfg"].state_dir).glob("*.mp4"))
    assert made and all(Path(home["cfg"].state_dir) in p.parents for p in made)


def test_a_guest_is_not_given_a_converted_copy(home):
    assert home["guest"].get(f"/api/stream/{home['ids']['camcorder.mkv']}").status_code == 403
    assert home["app"].test_client().get(
        f"/api/stream/{home['ids']['camcorder.mkv']}").status_code in (401, 403)


def test_asking_what_it_is_does_not_start_an_encode(home, monkeypatch):
    called = []
    monkeypatch.setattr(proxies, "live", lambda *a, **k: called.append(a))
    answer = home["family"].head(f"/api/stream/{home['ids']['camcorder.mkv']}")
    assert answer.status_code == 200 and called == []


def test_a_photograph_has_no_stream(home, scanned):
    _, conn, _ = scanned
    picture = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()["id"]
    assert home["family"].get(f"/api/stream/{picture}").status_code == 404
