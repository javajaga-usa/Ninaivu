"""A built portable Ninaivu, checked end to end the way a household uses it.

    python tools/check_portable.py check DOWNLOAD [--work DIR] [--no-ai]
    python tools/check_portable.py pair DOWNLOAD [--work DIR]
    python tools/check_portable.py secondary DOWNLOAD --referee-port N [--work DIR]
    python tools/check_portable.py primary DOWNLOAD --partner HOST --referee-port N [--work DIR]

DOWNLOAD is what a release attaches (the Windows .zip, the Mac .zip, a Linux
.tar.gz) or a folder one was unpacked to. Only the standard library here, and
the Python inside the download for everything else: nothing on the machine
running this is what is being checked.

``check`` is one computer, from nothing:

1. the download unpacks to one folder, and its Python is the one
   installers/python-version.txt names, the same on every system;
2. what the system would otherwise need installed is inside it, and works:
   ffmpeg and ffprobe (with every ffmpeg on the PATH taken away), iPhone
   photos (HEIC, written and read back), Tk for the Control Panel, OpenCV,
   RAW, TLS without an openssl program, Tamil text layout, and pip;
3. the first start: the server comes up on a library it has never seen, the
   first administrator is made, and every photograph and the video are
   indexed, with the capture date from EXIF, a thumbnail each, the video's
   length and poster, and names in Tamil and with accents;
4. the AI models page: ONNX Runtime installed from the Extras list into the
   folder's own Python, a model downloaded, and run once on a picture;
5. a second start on the same folder, and not one photograph changed.

``pair`` is Mugil's two computers on this one machine: the download unpacked
twice, one made the Secondary with a folder for the originals, the other the
Primary, paired with the code the Secondary shows, the originals sent, and
every one checked byte for byte where the Secondary keeps it.

``secondary`` and ``primary`` are the same between two machines, each
running one, reached over the tailnet (the release workflow joins both
runners to Tailscale): the Secondary serves its pairing code and, at the end,
its verdict on what arrived, on ``--referee-port``; the Primary finds it at
``--partner``, pairs, sends, and asks for that verdict. That is how a Mac
and a Linux PC, a Windows PC and a Linux PC, or a Mac and a Windows PC, are
checked together.

Exits 0 when every step held; otherwise says which did not, with the end of
each server's log.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"

#: Set by :func:`main`: where every program here starts, never a checkout,
#: whose own ninaivu/ would be imported instead of the download's.
WORK = Path(tempfile.gettempdir())

ADMIN = ("ci-admin", "a long CI password 2026!")
START_PATIENCE = 240.0
INDEX_PATIENCE = 300.0
AI_PATIENCE = 900.0
PAIR_PATIENCE = 1800.0

#: The library a first start meets. Names a family really uses: Tamil, an
#: accent, a space, a folder of a trip.
PHOTO_NAMES = [
    "2019/05/பாட்டி-80.jpg",
    "2019/05/café du matin.jpg",
    "Family Trip 2021/beach.jpg",
    "Family Trip 2021/screenshot.png",
    "2023/iPhone/IMG_0001.heic",
]
VIDEO_NAME = "2023/iPhone/clip.mp4"
EXIF_DAY = "2019-05-04"


class Failed(RuntimeError):
    pass


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def expect(condition: bool, what: str) -> None:
    if not condition:
        raise Failed(what)
    print(f"   ok: {what}", flush=True)


def wanted_python() -> str:
    for line in (REPO / "installers" / "python-version.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("PBS_PYTHON="):
            return line.split("=", 1)[1].strip()
    raise Failed("installers/python-version.txt names no PBS_PYTHON")


# ---------------------------------------------------------------------------
# The folder


def unpack(archive: Path, into: Path) -> Path:
    """The one folder *archive* unpacks to, inside *into*."""
    into.mkdir(parents=True, exist_ok=True)
    if archive.is_dir():
        target = into / archive.name
        shutil.copytree(archive, target, symlinks=True)
        return target
    staging = Path(tempfile.mkdtemp(prefix="unpack-", dir=into))
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as tar:
            tar.extractall(staging, filter="tar")
    elif archive.suffix == ".zip" and sys.platform == "darwin":
        # ditto keeps the app bundle's links and permissions; zipfile does not.
        subprocess.run(["ditto", "-x", "-k", str(archive), str(staging)], check=True)
    elif archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(staging)
    else:
        raise Failed(f"not a portable download: {archive}")
    made = [p for p in staging.iterdir() if p.name != "__MACOSX"]
    if WINDOWS and archive.suffix == ".zip":
        # The Windows zip holds Ninaivu.exe, app\ and logs\ at its top, as
        # Explorer's Extract All puts them into a folder named after it.
        names = sorted(p.name for p in made)
        expect(names == ["Ninaivu.exe", "app", "logs"], f"the zip holds Ninaivu.exe, app and logs ({names})")
        target = into / archive.stem
        staging.rename(target)
        return target
    if len(made) != 1 or not made[0].is_dir():
        raise Failed(f"{archive.name} unpacked to {[p.name for p in made]}, not one folder")
    target = into / made[0].name
    made[0].rename(target)
    shutil.rmtree(staging)
    return target


def without_ffmpeg(path: str) -> str:
    """PATH less every folder that holds an ffmpeg or ffprobe: what the
    download carries is what is being checked, not the runner's."""
    kept = []
    for folder in path.split(os.pathsep):
        if not folder:
            continue
        if any((Path(folder) / f"{tool}{'.exe' if WINDOWS else ''}").exists() for tool in ("ffmpeg", "ffprobe")):
            continue
        kept.append(folder)
    return os.pathsep.join(kept)


class Portable:
    """One unpacked download: its Python and what its launcher tells it."""

    def __init__(self, folder: Path, home: Path):
        self.folder = folder
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        base = {key: value for key, value in os.environ.items()
                if not key.startswith(("NINAIVU_", "PYTHON"))}
        base.update(NINAIVU_NO_DIALOG="1")
        if not WINDOWS:
            base["HOME"] = str(home)
        if WINDOWS:
            # As Ninaivu.exe sets them (installers/windows/portable/Ninaivu.cs).
            app, data = folder / "app", folder / "data"
            (data / "control").mkdir(parents=True, exist_ok=True)
            (folder / "logs").mkdir(exist_ok=True)
            base.update(NINAIVU_HOME=str(data / "control"), NINAIVU_STATE_DIR=str(data),
                        NINAIVU_AI_MODELS_DIR=str(data / "ai-models"),
                        NINAIVU_LOG_DIR=str(folder / "logs"), NINAIVU_PORTABLE="1",
                        NINAIVU_PORTABLE_ROOT=str(folder), NINAIVU_PROGRAM_DIR=str(app),
                        NINAIVU_LAUNCHER=str(folder / "Ninaivu.exe"), PYTHONNOUSERSITE="1")
            self.python = str(app / "python" / "python.exe")
            expect((app / "python" / "pythonw.exe").is_file(), "Ninaivu.exe's pythonw.exe is there")
        else:
            done = subprocess.run([str(self.launcher()), "--where"], env=base, cwd=str(WORK),
                                  capture_output=True, text=True, timeout=120)
            if done.returncode:
                raise Failed(f"{folder.name}: --where failed: {done.stderr.strip()}")
            told = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
            base.update(told)
            base.update(NINAIVU_PORTABLE="1", NINAIVU_PORTABLE_ROOT=str(folder), PYTHONNOUSERSITE="1")
            if (folder / "Ninaivu.app").is_dir():
                base["PYTHONDONTWRITEBYTECODE"] = "1"
            self.python = told["NINAIVU_PYTHON"]
        self.env = base
        self.state = Path(base["NINAIVU_STATE_DIR"])
        self.models = Path(base["NINAIVU_AI_MODELS_DIR"])

    def launcher(self) -> Path:
        for candidate in (self.folder / "ninaivu",
                          self.folder / "Ninaivu.app" / "Contents" / "MacOS" / "ninaivu"):
            if candidate.is_file():
                return candidate
        raise Failed(f"no Ninaivu launcher in {self.folder}")

    def run(self, code: str, *args: str, timeout: float = 300, env: dict | None = None) -> str:
        """*code* run by the download's own Python, isolated from this checkout."""
        done = subprocess.run([self.python, "-I", "-c", code, *args], env=env or self.env, cwd=str(WORK),
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        if done.returncode:
            raise Failed(f"the download's Python failed:\n{done.stdout}\n{done.stderr}")
        return done.stdout


# ---------------------------------------------------------------------------
# What is inside

INSIDE = r"""
import json, os, subprocess, sys, sysconfig
found = {"python": sys.version.split()[0], "executable": sys.executable}
from ninaivu.media import components
tools = components.bundled_tools_dir()
found["tools"] = str(tools) if tools else ""
for name in ("ffmpeg", "ffprobe"):
    where = components.find_tool(name)
    found[name] = where or ""
    if where:
        out = subprocess.run([where, "-hide_banner", "-version"], capture_output=True, text=True, timeout=60)
        found[name + "_version"] = (out.stdout.splitlines() or [""])[0]
import tkinter
found["tk"] = tkinter.Tcl().eval("info patchlevel")
import pillow_heif, PIL, cv2, rawpy, numpy, cryptography, zeroconf, blake3, psutil, waitress, exifread
from PIL import features
found["raqm"] = bool(features.check("raqm"))
found["pillow"] = PIL.__version__
found["heif"] = pillow_heif.__version__
found["libheif"] = pillow_heif.libheif_info().get("libheif", "")
import pip
found["pip"] = pip.__version__
import ssl, sqlite3
found["openssl"] = ssl.OPENSSL_VERSION
found["sqlite"] = sqlite3.sqlite_version
found["purelib"] = sysconfig.get_paths()["purelib"]
print(json.dumps(found))
"""

#: The Control Panel itself, built as Ninaivu.exe, Ninaivu.app and Open
#: Ninaivu open it, drawn for a moment, and closed without starting anything.
PANEL = r"""
import time, tkinter
from ninaivu.desktop import app
root = tkinter.Tk()
board = app.Dashboard(root)
end = time.monotonic() + 5
while time.monotonic() < end:
    root.update()
    time.sleep(0.05)
title = root.title()
root.destroy()
print("panel", tkinter.TkVersion, title)
"""

MAKE_LIBRARY = r"""
import json, os, subprocess, sys
from PIL import Image
import pillow_heif
pillow_heif.register_heif_opener()
root, names, video, ffmpeg, day = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
for i, name in enumerate(names):
    path = os.path.join(root, *name.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    picture = Image.frombytes("RGB", (640, 480), os.urandom(640 * 480 * 3))
    if name.endswith(".jpg"):
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = day.replace("-", ":") + " 10:%02d:00" % i
        picture.save(path, quality=90, exif=exif.tobytes())
    else:
        picture.save(path)
heic = [n for n in names if n.endswith(".heic")][0]
back = Image.open(os.path.join(root, *heic.split("/")))
assert back.size == (640, 480), back.size
path = os.path.join(root, *video.split("/"))
os.makedirs(os.path.dirname(path), exist_ok=True)
subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "testsrc=duration=3:size=320x240:rate=15", "-c:v", "mpeg4",
                "-pix_fmt", "yuv420p", "-y", path], check=True, timeout=120)
print("made")
"""

RUN_MODEL = r"""
import sys
import numpy as np
import onnxruntime
session = onnxruntime.InferenceSession(sys.argv[1], providers=["CPUExecutionProvider"])
given = session.get_inputs()[0]
dtype = np.float16 if "float16" in given.type else np.float32
picture = np.random.rand(1, 3, 32, 32).astype(dtype)
out = session.run(None, {given.name: picture})[0]
print(onnxruntime.__version__, list(out.shape))
"""


def check_inside(portable: Portable) -> dict:
    step("What the system would otherwise need installed is inside the download")
    # With every ffmpeg on the PATH taken away, as on a computer that has none.
    bare = {**portable.env, "PATH": without_ffmpeg(portable.env.get("PATH", ""))}
    found = json.loads(portable.run(INSIDE, env=bare).strip().splitlines()[-1])
    for key, value in found.items():
        print(f"   {key}: {value}")
    want = wanted_python()
    expect(found["python"] == want, f"its Python is {want}, the one every download carries")
    inside = str(portable.folder.resolve())
    expect(Path(found["executable"]).resolve().is_relative_to(inside), "and it is the folder's own")
    expect(Path(found["purelib"]).resolve().is_relative_to(inside),
           "packages install into the folder, not somewhere else on the computer")
    machine = portable.run("import platform; print(platform.machine())").strip().lower()
    if not (sys.platform.startswith("linux") and machine in ("aarch64", "arm64")):
        for name in ("ffmpeg", "ffprobe"):
            expect(bool(found[name]) and Path(found[name]).resolve().is_relative_to(inside),
                   f"{name} is the one the download carries ({found[name] or 'none found'})")
            expect(found.get(f"{name}_version", "").startswith(f"{name} version"), f"{name} runs")
    elif not found["ffmpeg"]:
        print("   (arm64 Linux: no ffmpeg of its own; the system's is used, and there is none here)")
    expect(found["tk"][:2] in ("8.", "9."), f"Tk {found['tk']} for the Control Panel")
    expect(bool(found["libheif"]), "libheif for iPhone photos")
    expect(found["raqm"], "Tamil text layout (raqm and FriBiDi)")
    expect(bool(found["pip"]), "pip, for the AI models page and Extras")
    if WINDOWS or sys.platform == "darwin" or os.environ.get("DISPLAY"):
        shown = portable.run(PANEL, timeout=120).strip().splitlines()[-1]
        expect(shown.startswith("panel"), f"the Control Panel opens and draws ({shown})")
    return found


# ---------------------------------------------------------------------------
# Talking to a running Ninaivu


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Client:
    """One signed-in browser, on one port."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: object | None = None, timeout: float = 60) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Content-Type": "application/json", "X-Requested-With": "ninaivu-check",
            "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as answer:
                raw, status = answer.read(), answer.status
                kind = answer.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            raw, status, kind = error.read(), error.code, error.headers.get("Content-Type", "")
        if "json" in kind:
            try:
                return status, json.loads(raw.decode("utf-8"))
            except ValueError:
                pass
        return status, raw

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    def post(self, path: str, body: object | None = None, **kw):
        return self.call("POST", path, {} if body is None else body, **kw)


def wait_for(what: str, test, patience: float, every: float = 2.0, server: "Server | None" = None):
    deadline = time.monotonic() + patience
    last = None
    while time.monotonic() < deadline:
        if server is not None:
            server.alive()
        try:
            last = test()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            last = exc
        else:
            if last:
                return last
        time.sleep(every)
    raise Failed(f"{what}: not after {patience:.0f} s (last: {last!r})")


class Server:
    def __init__(self, portable: Portable, photos: Path | None, log: Path, *, public: bool = False):
        self.portable = portable
        self.port, self.admin_port = free_port(), free_port()
        self.log = log
        command = [portable.python, "-m", "ninaivu"]
        if photos is not None:
            command.append(str(photos))
        command += ["--http", "--no-mdns", "--strict-port", "--port", str(self.port),
                    "--admin-port", str(self.admin_port), "--ai", "off", "--no-watch", "--allow-sleep"]
        if not public:
            command.append("--local-only")
        log.parent.mkdir(parents=True, exist_ok=True)
        self.out = open(log, "ab")
        print(f"   starting: {' '.join(command)}", flush=True)
        self.process = subprocess.Popen(command, env=portable.env, cwd=str(WORK),
                                        stdout=self.out, stderr=subprocess.STDOUT)
        self.family = Client(f"http://127.0.0.1:{self.port}")
        self.console = Client(f"http://127.0.0.1:{self.admin_port}")
        wait_for("the server answering /healthz", self._healthy, START_PATIENCE, 1.0, server=self)

    def alive(self) -> None:
        if self.process.poll() is not None:
            raise Failed(f"the server ended (code {self.process.returncode}); see {self.log}")

    def _healthy(self) -> bool:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5) as answer:
            return answer.status == 200

    def sign_in(self) -> None:
        status, body = self.console.post("/api/auth/setup", {
            "username": ADMIN[0], "password": ADMIN[1], "name": "CI"})
        if status == 409:
            status, body = self.console.post("/api/auth/login", {"username": ADMIN[0], "password": ADMIN[1]})
        expect(status == 200, f"signed in to the console as the administrator ({status} {body!r:.200})")
        status, body = self.family.post("/api/auth/login", {"username": ADMIN[0], "password": ADMIN[1]})
        expect(status == 200, f"and to the family app ({status} {body!r:.200})")

    def stop(self) -> None:
        if self.process.poll() is None:
            asked = False
            try:
                run = json.loads((self.portable.state / "ninaivu.run").read_text(encoding="utf-8"))
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.admin_port}/api/admin/shutdown",
                    data=json.dumps({"token": run["token"]}).encode(), method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=15) as answer:
                    asked = answer.status == 200
            except (OSError, ValueError, KeyError):
                asked = False
            try:
                self.process.wait(timeout=120 if asked else 5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                if asked:
                    self.out.close()
                    raise Failed(f"the server did not stop when asked; see {self.log}") from None
        self.out.close()


# ---------------------------------------------------------------------------
# One computer


def photo_hashes(photos: Path) -> dict[str, str]:
    return {path.relative_to(photos).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(photos.rglob("*"))
            if path.is_file() and not path.name.startswith(".")}


def make_library(portable: Portable, photos: Path, found: dict) -> None:
    ffmpeg = found.get("ffmpeg") or shutil.which("ffmpeg") or ""
    if not ffmpeg:
        raise Failed("no ffmpeg to make the test video with")
    portable.run(MAKE_LIBRARY, str(photos), json.dumps(PHOTO_NAMES), VIDEO_NAME, ffmpeg, EXIF_DAY)


def first_start(portable: Portable, photos: Path, logs: Path, found: dict) -> Server:
    step("The first start, on a library it has never seen")
    server = Server(portable, photos, logs / "first.log")
    server.sign_in()
    expected = len(PHOTO_NAMES) + 1
    import sqlite3
    db = portable.state / "index.db"

    def rows():
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT id, rel_path, kind, date_key, date_source, duration, thumb, width FROM assets")]

    def all_in():
        got = rows()
        ready = [r for r in got if r["thumb"] and (r["kind"] != "video" or (r["duration"] or 0) > 0)]
        return got if len(got) == expected and len(ready) == expected else None

    got = wait_for(f"all {expected} indexed with thumbnails", all_in, INDEX_PATIENCE, server=server)
    by_path = {r["rel_path"].replace("\\", "/"): r for r in got}
    for name in PHOTO_NAMES + [VIDEO_NAME]:
        print(f"   {name}: {by_path.get(name)}")
    expect(set(by_path) == set(PHOTO_NAMES) | {VIDEO_NAME},
           "every file is indexed under its own name, Tamil and accents too")
    for name in PHOTO_NAMES[:3]:
        expect(EXIF_DAY in (by_path[name]["date_key"] or "") or EXIF_DAY.replace("-", "") in (by_path[name]["date_key"] or ""),
               f"{name} is filed under the day in its EXIF")
    expect(by_path[PHOTO_NAMES[4]]["width"] == 640, "the iPhone photo (HEIC) was opened")
    video = by_path[VIDEO_NAME]
    expect(video["kind"] == "video" and video["duration"] and abs(video["duration"] - 3) < 1,
           f"the video is a video, {video['duration']} s long")
    for row in got:
        status, body = server.family.get(f"/api/thumb/{row['id']}")
        expect(status == 200 and isinstance(body, bytes) and len(body) > 200,
               f"the thumbnail of {row['rel_path']} is served")
    status, body = server.console.get("/api/admin/components")
    expect(status == 200, "the Extras list answers")
    listed = {c["id"]: c for c in body["components"]}
    expect(listed["heif"]["installed"], "Extras: iPhone photos are there")
    if found.get("ffmpeg"):
        expect(listed["ffmpeg"]["installed"], "Extras: ffmpeg is there, with nothing installed")
    return server


def ai_models(portable: Portable, server: Server) -> None:
    step("The AI models page: ONNX Runtime from Extras, a model, and a run")
    status, body = server.console.post("/api/admin/components/onnxruntime/install")
    expect(status in (200, 202), f"ONNX Runtime install started ({status} {body!r:.300})")

    def installed():
        status, body = server.console.get("/api/admin/components")
        entry = {c["id"]: c for c in body["components"]}["onnxruntime"]
        if entry["status"] == "failed" or entry.get("error"):
            raise Failed(f"ONNX Runtime did not install: {entry.get('error')}\n" + "\n".join(entry.get("log", [])[-30:]))
        return entry if entry["installed"] or entry["status"] == "installed" else None

    wait_for("ONNX Runtime installed", installed, AI_PATIENCE, 5.0, server=server)
    print("   ok: ONNX Runtime installed into the folder's Python")
    status, body = server.console.post("/api/admin/ai-models/upscale/download")
    expect(status in (200, 202), f"the upscale model's download started ({status})")

    def downloaded():
        status, body = server.console.get("/api/admin/ai-models")
        entry = {m["id"]: m for m in body["models"]}["upscale"]
        state = entry.get("download") or {}
        if state.get("status") in ("failed", "error"):
            raise Failed(f"the model did not download: {state}")
        return entry if entry["installed"] else None

    wait_for("the upscale model downloaded", downloaded, AI_PATIENCE, 5.0, server=server)
    model = next(portable.models.rglob("real_esrgan_x4_fp16.onnx"), None)
    expect(model is not None and model.resolve().is_relative_to(portable.folder.resolve())
           or (model is not None and str(portable.models) in str(model)),
           f"the model is kept in the folder's data ({model})")
    out = portable.run(RUN_MODEL, str(model), timeout=300).strip()
    expect("[1, 3, 128, 128]" in out, f"and it runs on this computer: {out}")


def check(download: Path, work: Path, *, ai: bool) -> None:
    logs, photos = work / "logs", work / "Photos"
    step(f"Unpacking {download.name}")
    folder = unpack(download, work / "Downloads")
    print(f"   {folder}")
    portable = Portable(folder, work / "home")
    found = check_inside(portable)
    make_library(portable, photos, found)
    before = photo_hashes(photos)
    expect(len(before) == len(PHOTO_NAMES) + 1, f"a library of {len(before)} files made")
    server = first_start(portable, photos, logs, found)
    try:
        status, _ = server.console.get("/api/admin/mugil")
        expect(status == 200, "the two-computer page answers")
        if ai:
            ai_models(portable, server)
    finally:
        server.stop()
    step("A second start, on the same folder")
    again = Server(portable, photos, logs / "second.log")
    try:
        again.sign_in()
        status, body = again.console.get("/api/admin/ai-models")
        expect(status == 200, "the AI models page opens")
        if ai:
            entry = {m["id"]: m for m in body["models"]}["upscale"]
            expect(entry["installed"] and not entry["missing_packages"],
                   "the model is ready, with what it needs installed")
    finally:
        again.stop()
    expect(photo_hashes(photos) == before, "not one photograph was changed")


# ---------------------------------------------------------------------------
# Two computers


def wait_indexed(portable: Portable, server: Server) -> None:
    import sqlite3
    db = portable.state / "index.db"
    expected = len(PHOTO_NAMES) + 1

    def counted():
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            return conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == expected

    wait_for(f"the Primary's library of {expected} indexed", counted, INDEX_PATIENCE, server=server)
    print(f"   ok: the Primary indexed its {expected} files", flush=True)


def make_secondary(server: Server, keep: Path) -> str:
    keep.mkdir(parents=True, exist_ok=True)
    status, body = server.console.post("/api/admin/mugil/role", {"role": "secondary", "originals_dir": str(keep)})
    expect(status == 200 and body.get("role") == "secondary", f"made the Secondary ({status} {body!r:.300})")
    status, body = server.console.post("/api/admin/mugil/code")
    expect(status == 200 and body.get("code"), "it shows a pairing code")
    return body["code"]


def pair_and_send(server: Server, partner_url: str, code: str) -> dict:
    status, body = server.console.post("/api/admin/mugil/role", {"role": "primary"})
    expect(status == 200 and body.get("role") == "primary", f"made the Primary ({status} {body!r:.300})")
    status, body = server.console.post("/api/admin/mugil/pair", {"url": partner_url, "code": code})
    expect(status == 200 and body.get("paired"), f"paired with {partner_url} ({status} {body!r:.300})")
    status, body = server.console.post("/api/admin/mugil/send")
    expect(status == 200, f"sending the originals ({status} {body!r:.300})")

    expected = len(PHOTO_NAMES) + 1

    def done():
        # What the Secondary confirmed it keeps, as the Primary wrote it down
        # (mugil-sent.db): a run may already have started by itself on
        # pairing, so the one asked for here can find nothing left to send.
        status, body = server.console.get("/api/admin/mugil")
        sending = body.get("sending") or {}
        counted = sum(int(c.get("files", 0)) for c in (body.get("confirmed") or {}).values())
        if not sending.get("running") and counted >= expected:
            return {"confirmed": counted, "last": sending.get("last")}
        failed = (sending.get("last") or {}).get("failed")
        if not sending.get("running") and failed:
            raise Failed(f"{failed} originals were not kept: {sending.get('last')}")
        return None

    result = wait_for("the originals sent and confirmed", done, PAIR_PATIENCE / 3, 3.0, server=server)
    print(f"   {result}")
    expect(result["confirmed"] == expected, f"the Secondary confirmed all {expected} originals")
    return result


def verdict(keep: Path, wanted: dict[str, str]) -> list[str]:
    """What is wrong with the originals the Secondary keeps, against *wanted*
    (path within the library: SHA-256)."""
    kept: dict[str, str] = {}
    for library in [p for p in keep.iterdir() if p.is_dir()] if keep.is_dir() else []:
        for path in library.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                kept[path.relative_to(library).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    problems = []
    for name, digest in sorted(wanted.items()):
        if name not in kept:
            problems.append(f"missing on the Secondary: {name}")
        elif kept[name] != digest:
            problems.append(f"different bytes on the Secondary: {name}")
    print(f"   the Secondary keeps {len(kept)} files: {sorted(kept)}")
    return problems


def pair(download: Path, work: Path) -> None:
    step(f"Two computers on this one: {download.name} unpacked twice")
    first = Portable(unpack(download, work / "primary"), work / "home-primary")
    second = Portable(unpack(download, work / "secondary"), work / "home-secondary")
    photos = work / "Photos"
    found = json.loads(first.run(INSIDE).strip().splitlines()[-1])
    make_library(first, photos, found)
    wanted = photo_hashes(photos)
    keep = work / "Originals on the Secondary"
    secondary = Server(second, None, work / "logs" / "secondary.log")
    primary = None
    try:
        secondary.sign_in()
        code = make_secondary(secondary, keep)
        primary = Server(first, photos, work / "logs" / "primary.log")
        primary.sign_in()
        wait_indexed(first, primary)
        pair_and_send(primary, f"http://127.0.0.1:{secondary.port}", code)
        problems = verdict(keep, wanted)
        expect(not problems, "every original is on the Secondary, byte for byte" + "".join(f"\n   {p}" for p in problems))
        expect(photo_hashes(photos) == wanted, "and not one was changed on the Primary")
    finally:
        if primary is not None:
            primary.stop()
        secondary.stop()


class Referee(http.server.ThreadingHTTPServer):
    """On the Secondary's machine: the code, for the Primary's to read, and
    the verdict on what arrived, once it says it has sent everything."""

    def __init__(self, port: int, code: str, keep: Path, family_port: int):
        self.code, self.keep, self.family_port, self.answer = code, keep, family_port, None
        self.finished = threading.Event()
        super().__init__(("0.0.0.0", port), RefereeHandler)


class RefereeHandler(http.server.BaseHTTPRequestHandler):
    server: Referee

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                                          # noqa: N802
        if self.path == "/code":
            self._send(200, {"code": self.server.code, "port": self.server.family_port,
                             "system": sys.platform})
        else:
            self._send(404, {})

    def do_POST(self):                                         # noqa: N802
        if self.path != "/done":
            return self._send(404, {})
        wanted = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        problems = verdict(self.server.keep, wanted["files"])
        self.server.answer = {"ok": not problems, "problems": problems, "system": sys.platform,
                              "from": wanted.get("system")}
        self._send(200, self.server.answer)
        self.server.finished.set()

    def log_message(self, *_):
        pass


def secondary_side(download: Path, work: Path, referee_port: int) -> None:
    step(f"The Secondary, on {sys.platform}: {download.name}")
    second = Portable(unpack(download, work / "secondary"), work / "home-secondary")
    keep = work / "Originals on the Secondary"
    server = Server(second, None, work / "logs" / "secondary.log", public=True)
    try:
        server.sign_in()
        code = make_secondary(server, keep)
        referee = Referee(referee_port, code, keep, server.port)
        threading.Thread(target=referee.serve_forever, daemon=True).start()
        print(f"   waiting for the Primary on port {referee_port}", flush=True)
        deadline = time.monotonic() + PAIR_PATIENCE
        while not referee.finished.wait(5):
            server.alive()
            if time.monotonic() > deadline:
                raise Failed("no Primary came")
        referee.shutdown()
        answer = referee.answer
        print(f"   {answer}")
        expect(answer["ok"], f"every original from {answer['from']} is here, byte for byte")
    finally:
        server.stop()


def primary_side(download: Path, work: Path, partner: str, referee_port: int) -> None:
    step(f"The Primary, on {sys.platform}, pairing with {partner}")
    first = Portable(unpack(download, work / "primary"), work / "home-primary")
    photos = work / "Photos"
    found = json.loads(first.run(INSIDE).strip().splitlines()[-1])
    make_library(first, photos, found)
    wanted = photo_hashes(photos)
    host = f"[{partner}]" if ":" in partner else partner

    def told():
        with urllib.request.urlopen(f"http://{host}:{referee_port}/code", timeout=10) as answer:
            return json.loads(answer.read().decode("utf-8"))

    shown = wait_for(f"the Secondary at {partner}", told, PAIR_PATIENCE, 10.0)
    print(f"   the Secondary runs on {shown['system']}")
    server = Server(first, photos, work / "logs" / "primary.log")
    try:
        server.sign_in()
        wait_indexed(first, server)
        pair_and_send(server, f"http://{host}:{shown['port']}", shown["code"])
        request = urllib.request.Request(
            f"http://{host}:{referee_port}/done", method="POST",
            data=json.dumps({"files": wanted, "system": sys.platform}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as answer:
            said = json.loads(answer.read().decode("utf-8"))
        expect(said["ok"], f"the Secondary ({said['system']}) has every original, byte for byte"
               + "".join(f"\n   {p}" for p in said["problems"]))
        expect(photo_hashes(photos) == wanted, "and not one was changed on the Primary")
    finally:
        server.stop()


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # The library's names are Tamil and accented, and a Windows console
    # prints in its code page, which has neither.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="what", required=True)
    for name in ("check", "pair", "secondary", "primary"):
        one = sub.add_parser(name)
        one.add_argument("download", type=Path)
        one.add_argument("--work", type=Path, default=None)
        if name == "check":
            one.add_argument("--no-ai", action="store_true", help="leave out the AI models step")
        if name in ("secondary", "primary"):
            one.add_argument("--referee-port", type=int, default=8765)
        if name == "primary":
            one.add_argument("--partner", required=True, help="the Secondary's address")
    args = parser.parse_args(argv)
    work = (args.work or Path(tempfile.mkdtemp(prefix=f"ninaivu-{args.what}-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    global WORK
    WORK = work
    download = args.download.resolve()
    try:
        if args.what == "check":
            check(download, work, ai=not args.no_ai)
        elif args.what == "pair":
            pair(download, work)
        elif args.what == "secondary":
            secondary_side(download, work, args.referee_port)
        else:
            primary_side(download, work, args.partner, args.referee_port)
    except (Failed, subprocess.SubprocessError, OSError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        for log in sorted((work / "logs").glob("*.log")):
            print(f"\n--- {log.name} (last lines)", file=sys.stderr)
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]), file=sys.stderr)
        return 1
    print("\nAll of it held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
