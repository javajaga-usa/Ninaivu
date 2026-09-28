#!/usr/bin/env python3
"""Run Ninaivu browser tests against a throwaway instance.

Starts a temporary Ninaivu instance with a live-photo fixture library,
waits for the family and admin apps to be ready, drives the browser test
suite (via Playwright / Node), and tears everything down cleanly.

Usage:
    python tests/run_browser_tests.py
    python tests/run_browser_tests.py tests/live_photos_ui.mjs
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

    Ported from `tests/archive_ui.sh`, which built this under /tmp and could
    only ever run on Linux. The shape is what the Archive tests assert about:
    eleven unique photographs across two cards, one byte-identical duplicate,
    one file with no extension that only a deep scan finds, and one file that
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


#: The libraries a test may ask for by name. "default" is what everything got
#: before any of this, and is still what a test that says nothing gets.
FIXTURES = {
    "default": build_fixture_library,
    "folders": build_folders_fixture,
    "visibility": build_visibility_fixture,
    "busy": build_busy_fixture,
    "several": build_several_fixture,
}


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
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"ninaivu-browser-{fixture}-"))
    state_dir = tmp_dir / "state"
    lib_dir = tmp_dir / "lib"
    lib_dir.mkdir(parents=True)
    FIXTURES[fixture](lib_dir)

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
        test_env["NINAIVU_SHOTS"] = str(shots)
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
                        help="Browser test files to run (default: every one)")
    args = parser.parse_args()
    chosen = args.tests or [str(p) for p in every_browser_test()]
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

