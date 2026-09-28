"""Playable copies of videos a browser cannot open.

A household archive is full of what camcorders wrote: AVI, MKV, WMV, MTS.
The viewer used to say "not playable" and stop. Now Ninaivu converts a copy.

The property that matters most is the one about the original, so it is tested
first and directly: the file on disk must come out of this byte for byte
unchanged. The archive promises never to re-encode your library, and a feature
that quietly re-encoded it in place would break that promise no matter how
convenient it was.
"""

import hashlib
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import ADMIN, GUEST, login
from ninaivu.utils import proxies
from ninaivu.utils.proxies import ProxyStore


HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")


def make_video(path: Path, seconds: int = 1, codec: str = "mpeg4") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
         "-c:v", codec, "-pix_fmt", "yuv420p", str(path), "-y"],
        check=True, capture_output=True)
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for(store: ProxyStore, asset_id: int, timeout: float = 60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.ready(asset_id):
            return store.ready(asset_id)
        if store.status(asset_id).state in ("failed", "unavailable"):
            return None
        time.sleep(0.15)
    return None


# --- which files need one --------------------------------------------------

@pytest.mark.parametrize("ext,expected", [
    ("mp4", True), ("avi", True), ("mkv", True), ("wmv", True),
    ("mts", True), ("flv", True), ("m2ts", True),
])
def test_camcorder_formats_are_recognised_as_needing_conversion(ext, expected):
    browser_native = {"mp4", "m4v", "webm", "mov"}
    assert proxies.needs_proxy(ext) is (ext not in browser_native)


@pytest.mark.parametrize("ext", ["mp4", "m4v", "webm", "mov"])
def test_formats_a_browser_already_plays_are_left_alone(ext):
    assert proxies.needs_proxy(ext) is False


def test_photographs_never_need_a_proxy():
    assert proxies.needs_proxy("avi", kind="picture") is False
    assert proxies.needs_proxy("jpg", kind="picture") is False


# --- the promise about the original ---------------------------------------

@needs_ffmpeg
def test_the_original_file_is_not_touched(tmp_path):
    """The one that would matter most if it were wrong."""
    source = make_video(tmp_path / "holiday.avi")
    before = digest(source)
    before_stat = source.stat()

    store = ProxyStore(tmp_path / "state")
    store.start(1, source)
    assert wait_for(store, 1) is not None

    assert digest(source) == before, "the source file was modified"
    assert source.stat().st_size == before_stat.st_size
    assert source.stat().st_mtime == before_stat.st_mtime


@needs_ffmpeg
def test_the_proxy_lands_outside_the_library(tmp_path):
    """A converted copy is a derivative like a thumbnail. It must not appear
    in the folder the scanner walks, or the next scan would index Ninaivu's own
    output as if it were somebody's home video."""
    library = tmp_path / "library"
    source = make_video(library / "holiday.avi")
    store = ProxyStore(tmp_path / "state")
    store.start(1, source)
    ready = wait_for(store, 1)

    assert ready is not None
    assert library not in ready.parents
    assert list(library.iterdir()) == [source]


@needs_ffmpeg
def test_the_copy_is_something_a_browser_can_play(tmp_path):
    source = make_video(tmp_path / "holiday.avi")
    store = ProxyStore(tmp_path / "state")
    store.start(1, source)
    ready = wait_for(store, 1)
    assert ready is not None

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", str(ready)],
        capture_output=True, text=True, check=True)
    assert "h264" in probe.stdout


# --- housekeeping ----------------------------------------------------------

def test_a_full_folder_drops_the_least_recently_played(tmp_path):
    """Least recently *used*, not oldest: the film watched every Christmas
    should outlive one somebody opened by accident."""
    store = ProxyStore(tmp_path / "state", cache_mb=1)
    directory = proxies.proxies_dir(tmp_path / "state")
    directory.mkdir(parents=True)

    for asset_id in (1, 2, 3):
        path = store.path_for(asset_id)
        path.write_bytes(b"x" * 400_000)          # 3 x 400 KB against a 1 MB cap
        time.sleep(0.02)

    # Asset 1 is the oldest by creation, but it is the one being watched.
    store.ready(1)
    time.sleep(0.02)

    store.evict()
    assert store.path_for(1).exists(), "the recently played copy was evicted"
    assert not store.path_for(2).exists()


def test_usage_reports_what_the_folder_holds(tmp_path):
    store = ProxyStore(tmp_path / "state", cache_mb=10)
    proxies.proxies_dir(tmp_path / "state").mkdir(parents=True)
    store.path_for(7).write_bytes(b"x" * 1024)

    usage = store.usage()
    assert usage["count"] == 1
    assert usage["bytes"] == 1024
    assert usage["cap_bytes"] == 10 * 1024 * 1024


def test_without_ffmpeg_it_says_so_rather_than_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: None)
    store = ProxyStore(tmp_path / "state")
    assert store.available is False

    state = store.start(1, tmp_path / "nothing.avi")
    assert state.state == "unavailable"
    assert "ffmpeg" in state.message


def test_a_half_written_copy_is_never_served(tmp_path):
    """Conversions land on a temporary name. A killed process must not leave
    something that looks ready."""
    store = ProxyStore(tmp_path / "state")
    directory = proxies.proxies_dir(tmp_path / "state")
    directory.mkdir(parents=True)
    (directory / "5-v1.9999.part.mp4").write_bytes(b"half a video")

    assert store.ready(5) is None
    assert store.usage()["count"] == 0


# --- who may ask for one ---------------------------------------------------

def test_a_guest_cannot_fetch_a_converted_video(app, people):
    """It is the same footage in a different container, so it follows the same
    rule as downloading."""
    client = app.test_client()
    login(client, *GUEST)
    assert client.get("/api/proxy/1").status_code in (401, 403, 404)


def test_an_anonymous_visitor_cannot_either(app, people):
    assert app.test_client().get("/api/proxy/1").status_code in (401, 403, 404)


def test_asking_for_a_photograph_is_a_404(app, people, scanned):
    _, conn, _ = scanned
    picture = conn.execute(
        "SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()
    client = app.test_client()
    login(client, *ADMIN)
    assert client.get(f"/api/proxy/{picture['id']}").status_code == 404
