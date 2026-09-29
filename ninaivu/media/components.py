"""The optional pieces, and fetching them from the console.

Ninaivu runs without any of these and says so where each one would have helped
— a video with no poster frame, an iPhone photo that will not open, a
photograph nobody has read the words in. That was honest and useless in equal
measure: the household was told what was missing and left to find a command
line to fix it, on every machine they ran Ninaivu on.

So the console fetches them. Three kinds, because they arrive in three ways:

* **A tool**, meaning ffmpeg. Not a Python package and not ours to
  redistribute, so it comes from the machine's own package manager — winget,
  Homebrew, apt, dnf — which is also what keeps it updated afterwards. Where
  there is no package manager Ninaivu says where to download it and where to
  put it, rather than pretending.
* **A package**, from an allowlist, installed with pip into the environment
  Ninaivu is already running in. Never an arbitrary name: the id chooses the
  pinned requirement, so nothing a request says can decide what is installed.
* **A model**, which Ninaivu already knows how to fetch, verify and install
  (media/model_catalog.py, media/faces.py, media/orientnet.py). Listed here so
  one page answers "what is missing", with the work left where it was.

Every install runs on its own thread and keeps its output, so the console can
show what happened rather than only whether it worked.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Lines of an install's output kept for the console. Enough to see what went
#: wrong; not a log file.
LOG_LINES = 200

#: How long an install may take before it is presumed stuck. A package manager
#: fetching ffmpeg over a slow line is minutes, not seconds.
INSTALL_TIMEOUT = 30 * 60


# ---------------------------------------------------------------------------
# Package managers
# ---------------------------------------------------------------------------

#: How to ask each package manager for something, and what it is called. In
#: preference order per platform: the first one present is used.
MANAGERS: dict[str, dict[str, Any]] = {
    "winget": {
        "label": "Windows Package Manager",
        "platforms": ("win32",),
        "command": lambda package: [
            "winget", "install", "--id", package, "-e", "--source", "winget",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity",
        ],
    },
    "brew": {
        "label": "Homebrew",
        "platforms": ("darwin",),
        "command": lambda package: ["brew", "install", package],
    },
    # apt and dnf need root, which Ninaivu does not have and must not ask for.
    # They are listed so the console can name the command to run instead.
    "apt-get": {
        "label": "apt",
        "platforms": ("linux",),
        "needs_root": True,
        "command": lambda package: ["apt-get", "install", "-y", package],
    },
    "dnf": {
        "label": "dnf",
        "platforms": ("linux",),
        "needs_root": True,
        "command": lambda package: ["dnf", "install", "-y", package],
    },
}


def available_manager(platform: str | None = None) -> str | None:
    """The package manager to use on this machine, or ``None``."""
    platform = platform or sys.platform
    for name, manager in MANAGERS.items():
        if not any(platform.startswith(p) for p in manager["platforms"]):
            continue
        if shutil.which(name):
            return name
    return None


# ---------------------------------------------------------------------------
# What can be fetched
# ---------------------------------------------------------------------------

def _ffmpeg_present() -> bool:
    from . import media                                   # noqa: PLC0415
    return bool(media.FFMPEG)


def _package_present(*modules: str) -> Callable[[], bool]:
    """Present when any of these import names is there.

    OCR answers to two of them — `rapidocr` now, `rapidocr_onnxruntime` on
    older installs — and either means the words can be read.
    """
    def present() -> bool:
        for module in modules:
            try:
                if importlib.util.find_spec(module) is not None:
                    return True
            except (ImportError, ValueError):
                continue
        return False
    return present


#: Everything the console offers, in the order it shows them.
CATALOGUE: dict[str, dict[str, Any]] = {
    "ffmpeg": {
        "kind": "tool",
        "label": "ffmpeg",
        "used_for": "Video poster frames, durations, and converting clips a "
                    "browser will not play. Also lets the Archive listen to a "
                    "sound file before it decides it is music.",
        "present": _ffmpeg_present,
        # Package names per manager, and where to get it by hand.
        "packages": {"winget": "Gyan.FFmpeg", "brew": "ffmpeg",
                     "apt-get": "ffmpeg", "dnf": "ffmpeg"},
        "manual": "https://ffmpeg.org/download.html",
        "restart": True,
    },
    "heif": {
        "kind": "package",
        "label": "iPhone photos (HEIC)",
        "used_for": "Opening the HEIC and HEIF photographs an iPhone takes.",
        "present": _package_present("pillow_heif"),
        "requirement": "pillow-heif>=0.15,<2",
        "restart": True,
    },
    "opencv": {
        "kind": "package",
        "label": "Video frames without ffmpeg",
        "used_for": "A second way to take a poster frame from a video, and the "
                    "face and orientation models' own reader.",
        "present": _package_present("cv2"),
        "requirement": "opencv-python-headless>=4.8,<5",
        "restart": True,
    },
    "exifread": {
        "kind": "package",
        "label": "A second EXIF reader",
        "used_for": "Capture dates Pillow cannot read, which the Archive uses "
                    "to file a photograph under the day it was taken.",
        "present": _package_present("exifread"),
        "requirement": "exifread>=3.0,<4",
        "restart": True,
    },
    "onnxruntime": {
        "kind": "package",
        "label": "Background removal and the other ONNX models",
        "used_for": "Running the models on the AI models page — background "
                    "removal, tidying up, upscaling and restoring. Without it "
                    "they download and then cannot be used.",
        "present": _package_present("onnxruntime"),
        # The plain build, which runs on the processor everywhere. The
        # DirectML build is faster on a Windows graphics card and answers to
        # the same import name, so a household that has installed that one
        # already shows as having this.
        "requirement": "onnxruntime>=1.17,<2",
        "restart": True,
    },
    "image-model": {
        "kind": "package",
        "label": "Search by description",
        "used_for": "The image model: tags, finding \"the beach at sunset\" without "
                    "anybody tagging it, and descriptions. About 2 GB installed, then "
                    "a one-time download of the model itself at the next start. On a "
                    "computer with less than 8 GB of memory and no graphics card "
                    "Ninaivu keeps the light search even with this installed.",
        "present": _package_present("open_clip"),
        "requirement": "open_clip_torch>=2.24,<4",
        # PyTorch first, from its own index: the processor-only build, which is
        # a fraction of the size of the default one with its graphics-card
        # libraries. The launcher's --ai auto does the same.
        "also": ["--index-url", "https://download.pytorch.org/whl/cpu", "torch>=2.0,<3"],
        "restart": True,
    },
    "ocr": {
        "kind": "package",
        "label": "Reading the words in photographs",
        "used_for": "Searching for what a sign, a menu or a screenshot says. "
                    "About 150 MB installed.",
        # Two package names answer to this; `rapidocr` is the one that
        # installs on Python 3.13 and later.
        "present": _package_present("rapidocr", "rapidocr_onnxruntime"),
        "requirement": "rapidocr>=3,<4",
        # Installed without its own dependency list, which is then installed
        # separately minus one entry.
        #
        # rapidocr names `opencv_python`. Ninaivu already has
        # `opencv-python-headless`, which is the same `cv2` module built
        # without a window system — so the requirement is already met in every
        # way that matters, but pip only knows distribution names and installs
        # the second build over the first. That fails on Windows every time,
        # because the face and orientation models have `cv2.pyd` loaded and
        # Windows will not replace an open file; and where it succeeds it
        # leaves two distributions owning one folder, so removing either
        # breaks the other. Leaving OpenCV out means pip never touches `cv2`,
        # and nothing has to be stopped to install this.
        #
        # The list is rapidocr 3.x's own, taken from its package metadata, less
        # `opencv_python`. The ONNX runtime is added: rapidocr runs its models
        # on it but does not declare it, and without it the package imports
        # and then reads nothing. A test holds this list against the metadata
        # of whatever rapidocr is installed.
        "no_deps": True,
        "also": ["pyclipper>=1.2.0", "numpy>=1.19.5,<3", "six>=1.15.0",
                 "Shapely>=1.7.1,!=2.0.4", "PyYAML", "Pillow", "tqdm",
                 "omegaconf!=2.2.1", "requests", "colorlog",
                 "onnxruntime>=1.17,<2"],
        "restart": True,
    },
}


def catalogue_entry(component_id: str) -> dict[str, Any]:
    entry = CATALOGUE.get(component_id)
    if entry is None:
        raise KeyError(component_id)
    return entry


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def _why_it_failed(state: dict[str, Any]) -> str:
    """Turn what the installer printed into something a household can act on.

    "the installer stopped with code 1" above forty lines of pip output is
    true and useless. The failure worth naming is the one that cannot be fixed
    by trying again: Windows will not replace a file that a running program
    has open, and the program holding it is Ninaivu. Installing the text reader
    pulls in a second build of OpenCV, which writes over the ``cv2`` the face
    and orientation models already have loaded, and the install dies on a
    permission error that has nothing to do with permissions.
    """
    tail = " ".join(state.get("log") or [])[-4000:]
    denied = ("WinError 5" in tail or "Access is denied" in tail
              or "Permission denied" in tail)
    if denied and "site-packages" in tail:
        # Not "install it again": this page is part of Ninaivu, so it is gone
        # the moment Ninaivu is stopped. The command it prints as "Runs:" is
        # the thing to run instead, by hand, while Ninaivu is not holding the
        # file open.
        return ("Ninaivu is using one of the files this would replace, and "
                "Windows will not overwrite a file that is open. Stop Ninaivu "
                "from the control panel, run the command shown above in a "
                "terminal, then start Ninaivu again.")
    if "No matching distribution" in tail or "Could not find a version" in tail:
        return ("No build of this exists for this machine's Python. Nothing "
                "here can fix that; the package has to catch up.")
    if "Read timed out" in tail or "Temporary failure in name resolution" in tail:
        return "The download timed out. It is worth trying again."
    return ""


class Installs:
    """The installs this process has run, and what they said."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}

    def state(self, component_id: str) -> dict[str, Any]:
        with self._lock:
            found = self._state.get(component_id)
            return dict(found) if found else {}

    def running(self, component_id: str) -> bool:
        return self.state(component_id).get("status") == "installing"

    def active(self) -> dict[str, dict[str, Any]]:
        """Every install running right now, for the activity strip."""
        with self._lock:
            return {component_id: dict(state)
                    for component_id, state in self._state.items()
                    if state.get("status") == "installing"}

    def _note(self, component_id: str, line: str) -> None:
        with self._lock:
            entry = self._state.setdefault(component_id, {"log": []})
            entry["log"] = (entry.get("log") or [])[-(LOG_LINES - 1):] + [line]

    def _finish(self, component_id: str, status: str, error: str = "") -> None:
        with self._lock:
            entry = self._state.setdefault(component_id, {"log": []})
            entry.update(status=status, error=error, finished_at=time.time())

    def start(self, component_id: str, command: list[str] | list[list[str]],
              on_done: Callable[[], None] | None = None,
              runner: Callable[..., Any] = subprocess.Popen) -> bool:
        """Run *command* in the background. False if it is already running.

        *command* is one command, or several to run in order. A step that
        fails ends the install there: the steps are ordered so that stopping
        early leaves nothing half in place.
        """
        steps = command if command and isinstance(command[0], list) else [command]
        with self._lock:
            if self._state.get(component_id, {}).get("status") == "installing":
                return False
            self._state[component_id] = {
                "status": "installing", "error": "", "started_at": time.time(),
                "log": [" ".join(steps[0])],
            }

        def run() -> None:
            code = 0
            for index, step in enumerate(steps):
                if index:
                    self._note(component_id, " ".join(step))
                try:
                    process = runner(step, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
                    for line in iter(process.stdout.readline, ""):
                        line = line.strip()
                        if line:
                            self._note(component_id, line)
                    code = process.wait(timeout=INSTALL_TIMEOUT)
                except Exception as exc:                       # noqa: BLE001
                    self._note(component_id, f"{type(exc).__name__}: {exc}")
                    self._finish(component_id, "failed", str(exc)[:300])
                    return
                if code != 0:
                    break
            if code == 0:
                self._finish(component_id, "installed")
            else:
                # The reason first, because it is the part somebody can act
                # on; the code stays on the end, because it is the part worth
                # quoting when they cannot.
                ending = f"the installer stopped with code {code}"
                reason = _why_it_failed(self._state.get(component_id, {}))
                self._finish(component_id, "failed",
                             f"{reason} ({ending})" if reason else ending)
            if on_done is not None:
                try:
                    on_done()
                except Exception:                          # noqa: BLE001
                    log.exception("could not re-check %s after installing",
                                  component_id)

        threading.Thread(target=run, name=f"install-{component_id}",
                         daemon=True).start()
        return True


installs = Installs()


def _stored_path() -> str:
    """The PATH as the machine has it written down, not as we inherited it.

    An installer adds its folder to the environment for *new* processes; this
    one keeps the PATH it started with, so a tool installed a minute ago is
    invisible to it. On Windows the real value is in the registry, and reading
    it is what lets an install count without restarting Ninaivu.
    """
    if not sys.platform.startswith("win"):
        return ""
    import winreg                                          # noqa: PLC0415

    parts = []
    for root, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
                if value:
                    parts.append(str(value))
        except OSError:
            continue
    return os.pathsep.join(parts)


def find_tool(name: str) -> str | None:
    """Where this tool is, looking beyond the PATH this process inherited."""
    found = shutil.which(name)
    if found:
        return found
    stored = _stored_path()
    if not stored:
        return None
    found = shutil.which(name, path=stored)
    if found:
        # Put it on this process's PATH as well, so anything that shells out
        # by name rather than by path finds it too.
        folder = str(Path(found).parent)
        if folder not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + folder
    return found


def refresh_tools() -> None:
    """Look for the tools again, so an install counts without a restart.

    ``shutil.which`` was read once at import, in three modules, and a poster
    frame taken a minute after installing ffmpeg would still have said it was
    not there.
    """
    from . import media                                   # noqa: PLC0415
    from ..archive import entertainment                   # noqa: PLC0415

    media.FFMPEG = find_tool("ffmpeg")
    media.FFPROBE = find_tool("ffprobe")
    entertainment.FFMPEG = media.FFMPEG


def install_command(component_id: str, manager: str | None = None) -> list[str]:
    """What to run to install this, or raise ValueError saying why not.

    The id chooses the command; nothing a request carries reaches it.
    """
    entry = catalogue_entry(component_id)
    if entry["kind"] == "package":
        command = [sys.executable, "-m", "pip", "install"]
        if entry.get("no_deps"):
            command.append("--no-deps")
        return command + [entry["requirement"]]
    if entry["kind"] == "tool":
        manager = manager or available_manager()
        if manager is None:
            raise ValueError(
                "This computer has no package manager Ninaivu can use, so "
                f"{entry['label']} has to be installed by hand.")
        if MANAGERS[manager].get("needs_root"):
            raise ValueError(
                f"Installing {entry['label']} with {MANAGERS[manager]['label']} "
                f"needs administrator rights, which Ninaivu does not have. Run "
                f"it yourself: sudo "
                + " ".join(MANAGERS[manager]["command"](entry["packages"][manager])))
        package = entry["packages"].get(manager)
        if not package:
            raise ValueError(
                f"{entry['label']} is not in {MANAGERS[manager]['label']}.")
        return MANAGERS[manager]["command"](package)
    raise ValueError(f"{entry['label']} is fetched on its own page.")


def install_steps(component_id: str, manager: str | None = None) -> list[list[str]]:
    """Every command an install runs, in order. Usually one.

    A package installed without its dependency list gets that list first, as
    its own step — so a failure there stops before the package itself arrives,
    rather than leaving something that shows as installed and cannot import.
    """
    entry = catalogue_entry(component_id)
    steps = []
    if entry["kind"] == "package" and entry.get("also"):
        steps.append([sys.executable, "-m", "pip", "install", *entry["also"]])
    steps.append(install_command(component_id, manager))
    return steps


def describe(component_id: str) -> dict[str, Any]:
    """One component, as the console shows it."""
    entry = catalogue_entry(component_id)
    state = installs.state(component_id)
    present = bool(entry["present"]())
    manager = available_manager() if entry["kind"] == "tool" else None
    try:
        steps = install_steps(component_id, manager)
        refusal = ""
    except ValueError as exc:
        steps = []
        refusal = str(exc)
    return {
        "id": component_id,
        "label": entry["label"],
        "kind": entry["kind"],
        "used_for": entry["used_for"],
        "installed": present,
        "status": "installed" if present else state.get("status", "missing"),
        "error": state.get("error", ""),
        "log": state.get("log", []),
        "installing": state.get("status") == "installing",
        # Every step, because the page prints this as "Runs:" and somebody who
        # has to run it by hand needs all of it, not the last line.
        "command": " && ".join(" ".join(step) for step in steps),
        "cannot": refusal,
        "manual": entry.get("manual", ""),
        "manager": MANAGERS[manager]["label"] if manager else "",
        # Imports happen once, at start, so a package that has just arrived is
        # only used after a restart.
        "needs_restart": bool(entry.get("restart")) and not present,
    }


def describe_all() -> list[dict[str, Any]]:
    return [describe(component_id) for component_id in CATALOGUE]


def install(component_id: str) -> tuple[bool, str]:
    """Start an install. Returns (started, why not)."""
    entry = catalogue_entry(component_id)
    if entry["present"]():
        return False, f"{entry['label']} is already installed."
    try:
        steps = install_steps(component_id)
    except ValueError as exc:
        return False, str(exc)
    if not installs.start(component_id, steps, on_done=refresh_tools):
        return False, f"{entry['label']} is already being installed."
    return True, ""
