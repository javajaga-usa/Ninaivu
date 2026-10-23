#!/usr/bin/env python3
"""Run Ninaivu browser tests against a throwaway instance.

Starts a temporary Ninaivu instance with a live-photo fixture library,
waits for the family and admin apps to be ready, drives the browser test
suite (via Playwright / Node), and tears everything down cleanly.

Usage:
    python tests/run_browser_tests.py
    python tests/run_browser_tests.py tests/live_photos_ui.mjs
    python tests/run_browser_tests.py --engine webkit
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path


def build_archive_fixture(work: Path) -> dict[str, str]:
    """Two "old drives" worth of photographs, and somewhere to file them.

    Ported from the old `tests/archive_ui.sh`, which built this under /tmp and could
    only ever run on Linux. The shape is what the Archive tests assert about:
    eleven unique photographs across two cards, one byte-identical duplicate,
    one file with no extension found only by its first bytes, and one file that
    is not media at all.

    Without Pillow there is nothing worth building — the tests that need this
    are skipped by their own assertions rather than by lying about the folders.
    """
    try:
        from PIL import Image
    except ImportError:
        return {}

    from datetime import datetime, timedelta

    card_a = work / "cardA" / "DCIM"
    card_b = work / "cardB" / "2016"
    for folder in (card_a, card_b):
        folder.mkdir(parents=True, exist_ok=True)

    def shot(path: Path, when, seed: int, size=(320, 240)) -> None:
        image = Image.new("RGB", size,
                          ((seed * 37) % 256, (seed * 91) % 256, (seed * 13) % 256))
        exif = image.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0110] = "TestCam"
        image.save(path, "JPEG", exif=exif)

    for i in range(6):
        shot(card_a / f"IMG_{i:04d}.jpg",
             datetime(2019, 3, 4) + timedelta(days=i * 220), i + 1)
    for i in range(5):
        shot(card_b / f"PIC_{i:04d}.jpg",
             datetime(2015, 7, 1) + timedelta(days=i * 260), 100 + i, (280, 210))

    shutil.copy(card_a / "IMG_0000.jpg", work / "cardB" / "same_photo_again.jpg")
    shutil.copy(card_b / "PIC_0002.jpg", work / "cardB" / "NOEXTENSION")
    (work / "cardA" / "readme.txt").write_text("not media", encoding="utf-8")

    # And the shoebox the selection tests sort through: photographs, video and
    # audio together, so turning a kind off has something to turn off.
    shoebox = work / "shoebox"
    shoebox.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        shot(shoebox / f"IMG_{i:04d}.jpg",
             datetime(2018, 5, 2) + timedelta(days=i * 90), i)
    for i in range(3):
        (shoebox / f"CLIP_{i}.mp4").write_bytes(b"\0" * (2 * 1024 * 1024))
    (shoebox / "song.mp3").write_bytes(b"\0" * (1024 * 1024))

    # A drive with enough on it that walking takes more than a second, which
    # is the only way to watch a walk that is still going.
    _many_photos(work / "olddrive" / "DCIM", BUSY_FILES)

    return {
        "NINAIVU_SRC_BIG": str(work / "olddrive"),
        "NINAIVU_SRC_A": str(work / "cardA"),
        "NINAIVU_SRC_B": str(work / "cardB"),
        "NINAIVU_SRC": str(shoebox),
        "NINAIVU_DEST": str(work / "Master"),
    }


def build_folders_fixture(root: Path) -> None:
    """What `folders_ui` asserts about: two years, two loose files, and a
    folder inside 2024 where everything is hidden.

    The shape is exactly what the test names — ["2023","2024"] at the top,
    two loose files beside them, three items nobody but an admin may see.
    """
    try:
        from PIL import Image
    except ImportError:
        return
    from datetime import datetime

    def shot(path: Path, seed: int, when=None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (320, 240),
                          ((seed * 37) % 256, (seed * 91) % 256, (seed * 13) % 256))
        exif = image.getexif()
        exif[0x9003] = (when or datetime(2023, 6, 1)).strftime("%Y:%m:%d %H:%M:%S")
        image.save(path, "JPEG", exif=exif)

    for i in range(3):
        shot(root / "2023" / f"IMG_{i:04d}.jpg", i, datetime(2023, 6, 1))
    for i in range(2):
        shot(root / "2024" / f"IMG_{i:04d}.jpg", 10 + i, datetime(2024, 2, 3))
    # The folder the test expects to be entirely out of sight.
    for i in range(3):
        shot(root / "2024" / "private" / f"IMG_{i:04d}.jpg", 20 + i,
             datetime(2024, 2, 4))
    # And two files loose in the root, which the test counts.
    shot(root / "loose_one.jpg", 31)
    shot(root / "loose_two.jpg", 32)


def build_visibility_fixture(root: Path) -> None:
    """A `personal` folder with three photographs in it, and some others.

    `visibility_ui` and `visibility_password_ui` hide that folder as an admin
    would and then check that nothing else was published along with it.
    """
    try:
        from PIL import Image
    except ImportError:
        return
    from datetime import datetime

    def shot(path: Path, seed: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (320, 240),
                          ((seed * 37) % 256, (seed * 91) % 256, (seed * 13) % 256))
        exif = image.getexif()
        exif[0x9003] = datetime(2022, 4, 5).strftime("%Y:%m:%d %H:%M:%S")
        image.save(path, "JPEG", exif=exif)

    for i in range(3):
        shot(root / "personal" / f"IMG_{i:04d}.jpg", 40 + i)
    # Nine altogether: the tests count the whole library, not just the part
    # they hide.
    for i in range(6):
        shot(root / "holiday" / f"IMG_{i:04d}.jpg", 50 + i)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_fixture_library(root: Path) -> None:
    """Create a minimal library with a live photo pair and standard photos."""
    try:
        from PIL import Image
    except ImportError:
        Image = None  # type: ignore

    live_dir = root / "2022" / "12"
    live_dir.mkdir(parents=True, exist_ok=True)
    other_dir = root / "2023" / "05"
    other_dir.mkdir(parents=True, exist_ok=True)

    jpg_path = live_dir / "IMG_0001.JPG"
    mov_path = live_dir / "IMG_0001.MOV"
    pic2_path = other_dir / "IMG_0002.JPG"

    if Image is not None:
        img = Image.new("RGB", (640, 480), (100, 140, 180))
        exif = img.getexif()
        exif[0x9003] = datetime(2022, 12, 15, 14, 30).strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0110] = "PhoneCam"
        img.save(jpg_path, "JPEG", exif=exif, quality=95)

        img2 = Image.new("RGB", (640, 480), (180, 140, 100))
        exif2 = img2.getexif()
        exif2[0x9003] = datetime(2023, 5, 20, 11, 15).strftime("%Y:%m:%d %H:%M:%S")
        img2.save(pic2_path, "JPEG", exif=exif2, quality=95)
    else:
        # Fallback minimal JPEG header
        header = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xFF\xDB\x00C\x00"
        jpg_path.write_bytes(header + b"\x00" * 70000)
        pic2_path.write_bytes(header + b"\x00" * 70000)

    # Pad if below 65 KB to ensure it passes any media floor
    if jpg_path.stat().st_size < 65000:
        with open(jpg_path, "ab") as f:
            f.write(b"\x00" * (65000 - jpg_path.stat().st_size))

    # The companion video fragment for the live photo
    mov_path.write_bytes(b"\x00" * 70000)


def wait_for_ready(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_for_scan(home_url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import json
            req = urllib.request.Request(f"{home_url}/api/status")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                stats = data.get("stats", {})
                if stats.get("live", 0) > 0 and stats.get("pictures", 0) > 0:
                    return
        except Exception:
            pass
        time.sleep(0.5)


#: Files that end in `_ui.mjs` are tests; everything else beside them is
#: something they import — the harness, the layout helpers, the timing
#: helpers. The convention is what makes "run all of them" safe to say.
def every_browser_test() -> list[Path]:
    """Every browser test there is, in a stable order.

    This used to be a list of three, written into an argparse default. There
    are forty-three .mjs files in that folder, and adding one to it did not
    make it run — it made it look like it ran. `first_load_ui.mjs` was
    written and never executed by anything; so were the tests for the
    archive, the cloud page, visibility and deleting.

    A test that is not run is worse than a test that does not exist, because
    the folder says the feature is covered.
    """
    here = Path(__file__).resolve().parent
    return sorted(p for p in here.glob("*_ui.mjs"))


#: The flows run on WebKit, Safari's engine, as well as Chromium (external audit
#: 2026-10-11, QA-01): signing in and the first load, Live Photos, choosing
#: photographs, a shared page, a password-protected album, the editor and the
#: picker. Not every suite: several drive Chromium itself (DevTools sessions,
#: Chromium switches), and smooth_scroll_ui's mouse-wheel checks do not hold
#: under headless WebKit on Linux, where a wheel over the gallery moves it 5 px
#: rather than 240 although the same wheel scrolls a bare page normally. That
#: one needs a look on a real Mac and iPhone, not a looser test here.
WEBKIT_FLOWS = (
    "first_load_ui.mjs", "live_photos_ui.mjs", "selection_ui.mjs", "share_page_ui.mjs",
    "visibility_password_ui.mjs", "editor_panels_ui.mjs", "picker_ui.mjs",
)


#: Where to keep Playwright's browsers when the default place cannot be read.
#: Anywhere outside AppData does; this is the path the warning below names, so
#: that following the advice is enough and nothing has to be set afterwards.
BROWSERS_WITHIN_REACH = r"C:\ms-playwright"


def browsers_the_tests_can_reach() -> str:
    """A Playwright browser folder this python's children can actually read.

    Empty when the usual place will do — the caller then leaves Playwright to
    find its own, which is right everywhere except the case below.
    """
    for candidate in (os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
                      BROWSERS_WITHIN_REACH):
        if candidate and Path(candidate).is_dir():
            return candidate
    return ""


def warn_if_the_browsers_are_out_of_reach() -> None:
    """Say so when the interpreter cannot see Playwright's browsers.

    Ninaivu's venv here is built on Microsoft Store Python. Everything a Store
    interpreter spawns inherits the app container's view of the filesystem,
    and that view hides parts of ``%LOCALAPPDATA%`` — including
    ``ms-playwright``. A normal process sees the folder; one spawned by this
    python does not, and Playwright then reports

        Executable doesn't exist at …chrome-headless-shell.exe

    about a file that is sitting right there, and advises `npx playwright
    install`, which downloads it again to the same unreadable place. That cost
    an afternoon once. If it is the case here, say which of the two fixes to
    reach for instead.
    """
    if (os.name != "nt" or os.environ.get("PLAYWRIGHT_BROWSER")
            or browsers_the_tests_can_reach()):
        return                      # already dealt with; saying so twice is noise
    browsers = Path(os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH")
        or Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright")
    if browsers.is_dir() or "WindowsApps" not in sys.base_prefix:
        return
    print(
        f"WARNING: {sys.executable}\n"
        f"  is a Microsoft Store python, and cannot see {browsers}.\n"
        "  Playwright will say its browser 'doesn't exist' when it does.\n"
        "  Either put the browsers somewhere outside AppData:\n"
        "    PLAYWRIGHT_BROWSERS_PATH=C:\\ms-playwright npx playwright install chromium\n"
        "  or rebuild the venv on a python.org interpreter.",
        file=sys.stderr)


def say_what_the_server_said(proc) -> None:
    """Print what Ninaivu printed, when it did not come up.

    Its output was captured and then dropped, so a run that failed to start
    said only "failed to start in time" — the traceback that explained it was
    sitting in a pipe nobody read. A runner that hides why the server did not
    start is a runner nobody can debug.
    """
    proc.terminate()
    try:
        output = proc.communicate(timeout=10)[0] or ""
    except Exception:                                    # noqa: BLE001
        return
    if not output.strip():
        print("  (the server printed nothing at all)", file=sys.stderr)
        return
    print("  --- what Ninaivu printed ---", file=sys.stderr)
    for line in output.strip().splitlines()[-25:]:
        print(f"  {line}", file=sys.stderr)


#: Enough files that a walk takes long enough to watch. Not a round number for
#: its own sake: below about a thousand the walk is over before a test can ask
#: what it is doing, and every extra file is paid on every run of this group.
BUSY_FILES = 3000


def _many_photos(folder: Path, count: int, start: int = 0) -> None:
    """`count` small JPEGs, written rather than drawn.

    Pillow costs about a millisecond an image, which is a minute and a half
    for a library this size on every run. These exist to be counted and walked,
    so one real JPEG is made and the rest are that file with a different tail —
    different bytes, different hash, same negligible cost.
    """
    try:
        from PIL import Image
    except ImportError:
        return
    folder.mkdir(parents=True, exist_ok=True)
    seed = folder / "IMG_00000.jpg"
    Image.new("RGB", (160, 120), (90, 120, 200)).save(seed, "JPEG")
    body = seed.read_bytes()
    for i in range(1, count):
        # A JPEG reader stops at the end-of-image marker, so trailing bytes are
        # ignored by everything that opens these and still change the file.
        (folder / f"IMG_{start + i:05d}.jpg").write_bytes(
            body + f"//{start + i}".encode())


def build_busy_fixture(root: Path) -> None:
    """A library big enough that a scan of it can be watched running."""
    for year in ("2019", "2020", "2021"):
        _many_photos(root / year, BUSY_FILES // 3)


def build_several_fixture(root: Path) -> None:
    """Half a dozen plain photographs, and nothing clever.

    The default library is a live-photo pair and one other file, which is the
    right shape for testing live photos and too small for anything that wants
    to delete two things and still have a gallery left. `recycle` waits for
    four cells and would have waited for ever.
    """
    _many_photos(root / "2022", 6)


def _clip(path: Path, when: str, seconds: float = 2.0) -> None:
    """A short real clip, as a phone's live photo has, dated *when* (UTC).

    Zeros when there is no ffmpeg: still a clip by name, just not one that
    plays, and the suite that wants it playing says so.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        done = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
             "-i", f"testsrc=size=640x480:rate=24:duration={seconds}",
             # VP9 rather than H.264: the Chromium the tests drive has no
             # H.264, and the point is to see the clip play.
             "-c:v", "libvpx-vp9", "-b:v", "400k", "-pix_fmt", "yuv420p",
             "-metadata", f"creation_time={when}", "-f", "mp4", str(path)],
            capture_output=True, check=False)
        if done.returncode == 0:
            return
    path.write_bytes(b"\x00" * 70000)


def build_pairs_fixture(root: Path) -> None:
    """Live photos and a RAW+JPEG shot, the way they arrive from a library.

    One live photo whose clip was filed under the next day (an iPhone dates
    the clip in UTC), one whose clip sits beside it, a CR2 with its JPEG, and
    a plain photograph.
    """
    try:
        from PIL import Image
    except ImportError:
        return
    import io

    def still(path: Path, when: datetime, colour) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (640, 480), colour)
        exif = image.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0110] = "PhoneCam"
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", exif=exif, quality=92)
        path.write_bytes(buffer.getvalue())
        return buffer.getvalue()

    still(root / "2022/12/15/IMG_0101.JPG", datetime(2022, 12, 15, 19, 30), (90, 140, 200))
    (root / "2022/12/16").mkdir(parents=True, exist_ok=True)
    _clip(root / "2022/12/16/IMG_0101.MOV", "2022-12-16T01:00:01Z")
    still(root / "2022/12/15/IMG_0102.JPG", datetime(2022, 12, 15, 9, 0), (200, 150, 90))
    _clip(root / "2022/12/15/IMG_0102.MOV", "2022-12-15T09:00:00Z")
    jpeg = still(root / "2023/05/20/IMG_0201.JPG", datetime(2023, 5, 20, 11, 15), (60, 160, 110))
    # A RAW as a camera lays one out: a TIFF header and the full-size preview.
    (root / "2023/05/20/IMG_0201.CR2").write_bytes(
        b"II*\x00" + b"\x00" * 64 + jpeg + b"\x00" * 4096)
    still(root / "2023/05/20/IMG_0300.JPG", datetime(2023, 5, 20, 12, 0), (170, 90, 140))


def build_year_fixture(root: Path) -> None:
    """A year with something to tell: three dozen photographs through 2023,
    with shapes in them so they are sharp enough to be picked, and a few
    from 2022 for the year before. For Our Year (our_year_ui.mjs)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    import random

    rng = random.Random(2023)
    shots = [(datetime(2023, 1 + i % 12, 3 + (i * 7) % 24, 9 + i % 9), i) for i in range(36)]
    shots += [(datetime(2022, 6, 1 + i, 10), 100 + i) for i in range(3)]
    for when, seed in shots:
        folder = root / when.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (800, 600), (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
        draw = ImageDraw.Draw(image)
        for _ in range(40):
            x, y = rng.randrange(800), rng.randrange(600)
            draw.ellipse((x, y, x + rng.randrange(20, 160), y + rng.randrange(20, 160)),
                         fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)),
                         outline=(0, 0, 0), width=3)
        exif = image.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0110] = "PhoneCam"
        image.save(folder / f"IMG_{seed:04d}.JPG", "JPEG", exif=exif, quality=90)


def build_decades_fixture(root: Path) -> None:
    """Thirty-two years of a family, a few photographs in each, so the Jump to
    list has far more years than its panel is tall (jump_years_ui.mjs)."""
    try:
        from PIL import Image
    except ImportError:
        return
    for year in range(1995, 2027):
        for month in (2, 7, 11):
            when = datetime(year, month, 10, 12)
            folder = root / when.strftime("%Y/%m/%d")
            folder.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGB", (320, 240), ((year * 7) % 256, (month * 23) % 256, 140))
            exif = image.getexif()
            exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
            image.save(folder / f"IMG_{year}{month:02d}.JPG", "JPEG", exif=exif)


def build_moments_fixture(root: Path) -> None:
    """A few photographs and one half-minute clip that plays, for the
    moments inside videos (video_moments_ui.mjs) and the words spoken in
    them (video_words_ui.mjs)."""
    _many_photos(root / "2023", 4)
    folder = root / "2023/08/15"
    folder.mkdir(parents=True, exist_ok=True)
    _clip(folder / "birthday.mp4", "2023-08-15T18:00:00Z", seconds=30)


def build_then_now_fixture(root: Path) -> None:
    """Drawn pairs years apart, for Then and now (then_and_now_ui.mjs): a
    temple tank in 1994 and 2021, a doorway in 1988 and 2019, Amma as a girl
    in 1966 and Arun as a boy in 1996 (each face's eyes where the test says),
    the older of each faded as an old print is."""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return

    def save(image: Image.Image, when: datetime, name: str, old: bool) -> None:
        if old:
            grey = image.convert("L").filter(ImageFilter.GaussianBlur(1.2))
            image = Image.merge("RGB", [grey.point(lambda v: min(255, int(v * 1.05 + 30))),
                                        grey.point(lambda v: int(v * 0.92 + 22)),
                                        grey.point(lambda v: int(v * 0.72 + 14))])
        folder = root / when.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        exif = image.getexif()
        exif[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        image.save(folder / name, "JPEG", exif=exif, quality=90)

    def tank(trees: int) -> Image.Image:
        image = Image.new("RGB", (1200, 900), (120, 170, 220))
        draw = ImageDraw.Draw(image)
        for y in range(0, 420):
            draw.line((0, y, 1200, y), fill=(120 + y // 6, 170 + y // 10, 225))
        draw.polygon([(420, 420), (600, 90), (780, 420)], fill=(196, 120, 70))
        for k in range(5):
            draw.line((440 + k * 36, 400 - k * 60, 760 - k * 36, 400 - k * 60), fill=(120, 60, 30), width=6)
        draw.rectangle((0, 420, 1200, 520), fill=(200, 180, 140))
        for k in range(8):
            draw.rectangle((0, 520 + k * 14, 1200, 532 + k * 14), fill=(180 - k * 6, 160 - k * 6, 120))
        draw.rectangle((0, 640, 1200, 900), fill=(40, 110, 130))
        for x in range(60, 1200, 1200 // max(1, trees)):
            draw.rectangle((x + 20, 300, x + 34, 420), fill=(90, 60, 30))
            draw.ellipse((x - 30, 220, x + 84, 330), fill=(40, 120, 50))
        return image

    def door(colour: tuple[int, int, int], lamps: bool) -> Image.Image:
        image = Image.new("RGB", (1200, 900), (230, 214, 180))
        draw = ImageDraw.Draw(image)
        draw.rectangle((420, 160, 780, 760), fill=colour)
        draw.rectangle((450, 190, 750, 740), outline=(60, 30, 10), width=10)
        draw.rectangle((0, 760, 1200, 900), fill=(150, 90, 70))
        for k in range(9):
            draw.ellipse((300 + k * 70, 790, 340 + k * 70, 830), outline=(255, 255, 255), width=4)
        if lamps:
            for x in (360, 820):
                draw.ellipse((x, 700, x + 40, 740), fill=(250, 190, 60))
        return image

    def face(size: float, at: tuple[float, float], hair: tuple[int, int, int]) -> Image.Image:
        image = Image.new("RGB", (900, 1200), (210, 190, 160))
        draw = ImageDraw.Draw(image)
        w, h = image.size
        cx, cy = at[0] * w, at[1] * h
        r = size * w
        draw.ellipse((cx - r * 1.1, cy - r * 1.4, cx + r * 1.1, cy + r * 0.6), fill=hair)
        draw.ellipse((cx - r, cy - r * 1.1, cx + r, cy + r * 1.2), fill=(200, 150, 110))
        draw.rectangle((cx - r * 1.4, cy + r * 1.2, cx + r * 1.4, h), fill=(170, 60, 80))
        for ex in (-0.4, 0.4):
            draw.ellipse((cx + ex * r - r * 0.12, cy - r * 0.12, cx + ex * r + r * 0.12, cy + r * 0.12),
                         fill=(30, 20, 15))
        draw.arc((cx - r * 0.4, cy + r * 0.3, cx + r * 0.4, cy + r * 0.8), 20, 160, fill=(120, 40, 40), width=6)
        return image

    save(tank(3), datetime(1994, 6, 18, 9), "TANK_1994.JPG", True)
    save(tank(6), datetime(2021, 6, 20, 9), "TANK_2021.JPG", False)
    save(door((150, 40, 30), False), datetime(1988, 6, 17, 18), "DOOR_1988.JPG", True)
    save(door((30, 90, 140), True), datetime(2019, 6, 19, 18), "DOOR_2019.JPG", False)
    # Eyes at x 0.4 ± 0.4·size, y 0.35 (Amma); x 0.6 ± 0.4·size, y 0.45 (Arun).
    save(face(0.12, (0.4, 0.35), (30, 20, 20)), datetime(1966, 6, 15, 11), "AMMA_1966.JPG", True)
    save(face(0.16, (0.6, 0.45), (25, 25, 30)), datetime(1996, 6, 16, 11), "ARUN_1996.JPG", False)


#: The libraries a test may ask for by name. "default" is what everything got
#: before any of this, and is still what a test that says nothing gets.
FIXTURES = {
    "default": build_fixture_library,
    "folders": build_folders_fixture,
    "visibility": build_visibility_fixture,
    "busy": build_busy_fixture,
    "several": build_several_fixture,
    "pairs": build_pairs_fixture,
    "year": build_year_fixture,
    "decades": build_decades_fixture,
    "moments": build_moments_fixture,
    "thennow": build_then_now_fixture,
}


def is_manual(path: Path) -> bool:
    """A test marked `@manual` in its header is run by hand, not by this runner.

    For a suite whose library cannot be built here: `orientation_ui` needs real
    photographs of people the right way up and on their side, and the AI models
    that judge them. Saying so, out loud, is better than failing every run for a
    reason that has nothing to do with the code - or quietly not running it.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2500]
    except OSError:
        return False
    return re.search(r"@manual\b", head) is not None


def fixture_wanted(path: Path) -> str:
    """Which library a test asks for, from `// @fixture <name>` in its header."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return "default"
    match = re.search(r"@fixture\s+([a-z-]+)", head)
    name = match.group(1) if match else "default"
    return name if name in FIXTURES else "default"


def run_group(fixture: str, tests: list[Path], node: str,
              home_dir: Path) -> bool:
    """Start one Ninaivu on one library, run these tests, stop it again."""
    # Resolved: on a Mac the temporary folder is under /var, a link to /private/var, and
    # the server stores the resolved path — so a test comparing the path it was
    # given with the one the server reports would never match.
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"ninaivu-browser-{fixture}-")).resolve()
    state_dir = tmp_dir / "state"
    lib_dir = tmp_dir / "lib"
    lib_dir.mkdir(parents=True)
    FIXTURES[fixture](lib_dir)

    # A household that has been using Ninaivu has been through the first-day
    # walk-through. A fresh state folder shows it over the console, and it
    # covers every button the tests then try to press.
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "config.json").write_text('{"first_day_done": true}', encoding="utf-8")

    home_port = find_free_port()
    admin_port = find_free_port()

    server_env = os.environ.copy()
    server_env["NINAIVU_STATE_DIR"] = str(state_dir)
    server_env["PYTHONUNBUFFERED"] = "1"
    # The fixture library is drawn at a few kilobytes a file, well under the
    # 60 KB floor that keeps icons and email signatures out of a real library.
    # This used to be passed as `--min-media-bytes 0`, which `ninaivu` has no
    # such flag for — so the server exited on its own usage message and every
    # browser run died before a single test, saying only "failed to start in
    # time". It is read from the environment (see config.Config.load).
    server_env["NINAIVU_MIN_MEDIA_BYTES"] = "0"
    # Whether a scan is followed by a straightening survey depends on whether
    # this machine happens to have the orientation model — it would run for
    # minutes over the fixture on a machine that does, holding the disk, and not
    # at all on one that does not. A test should not depend on which it is; the
    # tests that are about straightening start it themselves.
    server_env["NINAIVU_STRAIGHTEN_AUTO"] = "0"

    cmd = [
        sys.executable, "-m", "ninaivu", str(lib_dir),
        "--ai", "off", "--no-watch", "--admin", "dad:correcthorse1",
        "--port", str(home_port), "--admin-port", str(admin_port),
    ]

    print(f"\nStarting Ninaivu on the '{fixture}' library "
          f"(home: {home_port}, admin: {admin_port})...", flush=True)
    proc = subprocess.Popen(cmd, cwd=str(home_dir), env=server_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        home_url = f"http://127.0.0.1:{home_port}"
        admin_url = f"http://127.0.0.1:{admin_port}"
        if not wait_for_ready(f"{home_url}/api/status"):
            print("ERROR: Home app failed to start in time", file=sys.stderr)
            say_what_the_server_said(proc)
            return False
        if not wait_for_ready(f"{admin_url}/"):
            print("ERROR: Admin console failed to start in time", file=sys.stderr)
            say_what_the_server_said(proc)
            return False

        print("Ninaivu is ready. Waiting for initial scan...", flush=True)
        wait_for_scan(home_url)

        test_env = os.environ.copy()
        # Where the fixtures are, and where screenshots go. Both used to be
        # hard-coded /tmp paths written on a Linux machine.
        test_env.update(build_archive_fixture(tmp_dir))
        shots = tmp_dir / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        test_env["NINAIVU_SHOTS"] = os.environ.get("NINAIVU_SHOTS") or str(shots)
        reachable = browsers_the_tests_can_reach()
        if reachable:
            test_env["PLAYWRIGHT_BROWSERS_PATH"] = reachable
        test_env["NINAIVU_HOME"] = home_url
        test_env["NINAIVU_ADMIN"] = admin_url
        test_env["NINAIVU_USER"] = "dad"
        test_env["NINAIVU_PASS"] = "correcthorse1"

        all_ok = True
        for target in tests:
            # Flushed: each test writes straight to this stream, so a buffered
            # heading lands after every test it was meant to introduce. The log
            # then cannot be read a failure out of, and the log is the whole
            # point of running these.
            print(f"\n--- Running {target} ---", flush=True)
            res = subprocess.run([node, str(target)], cwd=str(home_dir),
                                 env=test_env)
            if res.returncode != 0:
                print(f"FAIL {target} (exit code {res.returncode})", flush=True)
                all_ok = False
            else:
                print(f"PASS {target}", flush=True)
        return all_ok
    finally:
        print(f"\nStopping the '{fixture}' instance...", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", default=None,
                        help="Browser test files to run (default: every one, or on "
                             "another engine the flows in WEBKIT_FLOWS)")
    parser.add_argument("--engine", choices=("chromium", "webkit", "firefox"),
                        default=os.environ.get("PLAYWRIGHT_ENGINE", "chromium"),
                        help="Playwright browser engine (default chromium)")
    args = parser.parse_args()
    os.environ["PLAYWRIGHT_ENGINE"] = args.engine
    here = Path(__file__).resolve().parent
    if args.tests:
        chosen = args.tests
    elif args.engine == "chromium":
        chosen = [str(p) for p in every_browser_test()]
    else:
        chosen = [str(here / name) for name in WEBKIT_FLOWS]
    if not chosen:
        print("ERROR: no browser tests found", file=sys.stderr)
        return 1

    node = shutil.which("node")
    if not node:
        print("ERROR: node is not installed on PATH", file=sys.stderr)
        return 1

    warn_if_the_browsers_are_out_of_reach()
    home_dir = Path(__file__).resolve().parents[1]

    # Group by the library each test asks for. A test that asks for nothing
    # gets the default one, which is every test that existed before this.
    groups: dict[str, list[Path]] = {}
    for name in chosen:
        target = Path(name)
        if not target.is_absolute():
            target = home_dir / name
        if not target.is_file():
            print(f"SKIP {name} (not found)")
            continue
        if is_manual(target):
            print(f"SKIP {name} (marked @manual: it needs a library this runner cannot build)")
            continue
        groups.setdefault(fixture_wanted(target), []).append(target)

    if not groups:
        return 1

    all_ok = True
    for fixture in sorted(groups):
        if not run_group(fixture, groups[fixture], node, home_dir):
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

