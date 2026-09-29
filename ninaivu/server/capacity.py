"""What this computer can do for Ninaivu, and what would help it do more.

The Performance page in the console. It measures rather than guesses: the
processor and its kinds of core, the memory and how much of it is in use, the
graphics processor and whether the image model is on it, each drive Ninaivu
reads from and what it is (its file system, how it is connected, whether it can
be written), how long a scan with nothing new actually took, and how much
analysis is waiting. Then it says what would make a difference, and offers the
change where Ninaivu can make it itself.

Two halves, kept apart on purpose. :func:`assess` gathers facts, which depends
on the machine it runs on. :func:`recommend` turns facts into advice and is a
plain function of them, so every rule is tested on every system — a Mac rule
on Windows, and the other way round.

Everything here was found on a real machine before it was written down: the
Mac this page was made for had its image model on the processor while an
idle graphics processor analysed photographs twice as fast, and its library on
a drive macOS mounts read-only — so nothing in the library could be changed
from the Mac, and nothing said so.
"""

from __future__ import annotations

import functools
import os
import platform
import plistlib
import subprocess
import sys
import time
from typing import Any

try:
    import psutil
except ImportError:                                      # pragma: no cover
    psutil = None

GB = 1024 ** 3

#: Advice, most pressing first. "act": something is wrong or holding Ninaivu
#: back. "consider": a change that would help. "good": checked and fine, said
#: so that a page of advice is not read as a page of problems.
LEVELS = ("act", "consider", "good")


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------

def _run(*command: str, timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _processor_name() -> str:
    if sys.platform == "darwin":
        name = _run("sysctl", "-n", "machdep.cpu.brand_string")
        if name:
            return name
    elif sys.platform == "win32":
        try:
            import winreg                                # noqa: PLC0415

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    else:
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as info:
                for line in info:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "Unknown processor"


def _core_kinds() -> list[dict[str, Any]]:
    """The kinds of core, fastest first, where the system says: a Mac's
    performance levels ("Super", "Performance", "Efficiency" on an M6)."""
    if sys.platform != "darwin":
        return []
    try:
        levels = int(_run("sysctl", "-n", "hw.nperflevels") or 0)
    except ValueError:
        return []
    kinds = []
    for level in range(levels):
        name = _run("sysctl", "-n", f"hw.perflevel{level}.name")
        count = _run("sysctl", "-n", f"hw.perflevel{level}.logicalcpu")
        if name and count.isdigit():
            kinds.append({"name": name, "cores": int(count)})
    return kinds


def _system_name() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    return f"{platform.system()} {platform.release()}".strip()


@functools.lru_cache(maxsize=1)
def machine() -> dict[str, Any]:
    """What the computer is. Measured once: none of it changes while running."""
    logical = os.cpu_count() or 1
    physical = psutil.cpu_count(logical=False) if psutil else None
    return {
        "system": _system_name(),
        "processor": _processor_name(),
        "logical_cores": logical,
        "physical_cores": physical or logical,
        "core_kinds": _core_kinds(),
        "memory_bytes": psutil.virtual_memory().total if psutil else None,
        "apple_silicon": sys.platform == "darwin" and platform.machine() == "arm64",
    }


def graphics(engine: Any) -> dict[str, Any]:
    """Which graphics processor there is, and whether the image model is on it.

    Asked of torch only when it is already loaded — importing it to answer a
    page is seconds and hundreds of megabytes. Without it, a Mac with Apple
    silicon still has one.
    """
    device = getattr(engine, "device", None)
    kind = name = None
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                kind, name = "cuda", torch.cuda.get_device_name(0)
        except Exception:                                # noqa: BLE001
            pass
        try:
            if kind is None and torch.backends.mps.is_available():
                kind = "mps"
        except Exception:                                # noqa: BLE001
            pass
    elif machine()["apple_silicon"]:
        kind = "mps"
    if kind == "mps":
        name = f"{machine()['processor']} graphics"
    return {"available": kind, "name": name, "ai_device": device,
            "in_use": device if device in ("cuda", "mps") else None}


# ---------------------------------------------------------------------------
# Drives
# ---------------------------------------------------------------------------

def _partition(path: str):
    """The mounted volume holding ``path``, matched by device rather than by
    name: on a Mac, /Users is on the Data volume, not on "/"."""
    if psutil is None:
        return None
    try:
        device = os.stat(path).st_dev
    except OSError:
        return None
    best = None
    for part in psutil.disk_partitions(all=False):
        try:
            if os.stat(part.mountpoint).st_dev != device:
                continue
        except OSError:
            continue
        if best is None or len(part.mountpoint) > len(best.mountpoint):
            best = part
    return best


def _mac_disk(mountpoint: str) -> dict[str, Any]:
    raw = _run("diskutil", "info", "-plist", mountpoint)
    if not raw:
        return {}
    try:
        info = plistlib.loads(raw.encode("utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}
    return {"bus": info.get("BusProtocol"), "solid_state": info.get("SolidState"),
            "internal": info.get("Internal")}


def storage(path: str, role: str) -> dict[str, Any]:
    """One place Ninaivu keeps things: its file system, connection and room."""
    out: dict[str, Any] = {"role": role, "path": path, "present": os.path.isdir(path)}
    if not out["present"]:
        return out
    part = _partition(path)
    opts = set((part.opts if part else "").split(","))
    out["filesystem"] = (part.fstype or "").lower() if part else None
    out["read_only"] = "ro" in opts or not os.access(path, os.W_OK)
    if psutil is not None:
        try:
            usage = psutil.disk_usage(path)
            out["free_bytes"], out["total_bytes"] = usage.free, usage.total
        except OSError:
            pass
    out.update({"bus": None, "solid_state": None, "internal": None})
    if part is not None and sys.platform == "darwin":
        out.update(_mac_disk(part.mountpoint))
    elif part is not None and sys.platform == "win32":
        out["internal"] = False if "removable" in opts else None
    return out


# ---------------------------------------------------------------------------
# Ninaivu itself
# ---------------------------------------------------------------------------

def scan_speed(conn, root: str) -> dict[str, Any] | None:
    """How long the last scan that found nothing new took, and for how many files."""
    row = conn.execute(
        "SELECT started_at, ended_at FROM scan_runs WHERE root=? AND status='done' "
        "AND ended_at IS NOT NULL AND added=0 AND updated=0 ORDER BY id DESC LIMIT 1",
        (root,)).fetchone()
    if not row:
        return None
    seconds = max(0.0, float(row[1]) - float(row[0]))
    files = conn.execute("SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0",
                         (root,)).fetchone()[0]
    return {"seconds": round(seconds, 1), "files": int(files),
            "files_per_second": round(files / seconds) if seconds else None,
            "at": float(row[1])}


def backlog(conn, cfg: Any) -> dict[str, int]:
    """Items still waiting for the slow passes, across every library folder."""
    from ..media.scanner import AI_VERSION                       # noqa: PLC0415
    from ..storage import db                                     # noqa: PLC0415

    from ..media import media                                    # noqa: PLC0415

    roots = list(getattr(cfg, "roots", []) or [])
    if not roots:
        return {"analysis": 0, "faces": 0}
    marks = ",".join("?" * len(roots))
    analysis = 0
    if getattr(cfg, "ai_enabled", False):
        # Only what the analysis can actually do: a photograph too damaged to
        # have a thumbnail is skipped by every pass, and counting it said
        # "156 waiting for the night" about files that will wait for ever.
        size, fmt = max(cfg.thumb_sizes), cfg.thumb_format
        for (thumb,) in conn.execute(
                f"SELECT thumb FROM assets WHERE root IN ({marks}) AND trashed=0 "
                f"AND kind != 'audio' AND thumb IS NOT NULL AND ai_version < ? LIMIT 200000",
                (*roots, AI_VERSION)):
            if (cfg.thumbs_dir / media.thumb_file(thumb, size, fmt)).is_file():
                analysis += 1
    faces = sum(db.face_stats(conn, root)["photos_pending"] for root in roots) \
        if getattr(cfg, "faces_enabled", False) else 0
    return {"analysis": int(analysis), "faces": int(faces)}


def load() -> dict[str, Any]:
    """How busy the computer is right now."""
    if psutil is None:
        return {}
    memory, swap = psutil.virtual_memory(), psutil.swap_memory()
    battery = None
    try:
        reading = psutil.sensors_battery()
        if reading is not None:
            battery = {"percent": reading.percent, "plugged": reading.power_plugged}
    except (AttributeError, NotImplementedError, OSError):
        pass
    try:
        me = psutil.Process(os.getpid())
        ninaivu = me.memory_info().rss + sum(
            child.memory_info().rss for child in me.children(recursive=True))
    except psutil.Error:
        ninaivu = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "memory_percent": memory.percent,
        "memory_available_bytes": memory.available,
        "swap_used_bytes": swap.used,
        "ninaivu_memory_bytes": ninaivu,
        "battery": battery,
    }


def assess(cfg: Any, conn, engine: Any) -> dict[str, Any]:
    """Every fact the page shows, and the advice that follows from them."""
    from ..media import media                                    # noqa: PLC0415
    from ..utils.resources import budget                         # noqa: PLC0415

    plan = budget()
    roots = list(getattr(cfg, "roots", []) or [])
    facts = {
        "platform": sys.platform,
        "machine": machine(),
        "graphics": graphics(engine),
        "load": load(),
        "storage": [storage(root, "library") for root in roots]
        + [storage(str(cfg.state_dir), "index")],
        "scans": {root: scan_speed(conn, root) for root in roots},
        "backlog": backlog(conn, cfg),
        "ninaivu": {
            "mode": plan["mode"],
            "workers": int(getattr(cfg, "workers", 0) or plan["workers"]),
            "compute_threads": plan["compute_threads"],
            "server_threads": plan["server_threads"],
            "schedule": str(getattr(cfg, "workload_mode", "balanced") or "balanced"),
            "night": [getattr(cfg, "workload_night_start", "23:00"),
                      getattr(cfg, "workload_night_end", "06:00")],
            "ai_enabled": bool(getattr(cfg, "ai_enabled", False)),
            "ai_gpu": bool(getattr(cfg, "ai_gpu", True)),
            "ai_model": getattr(engine, "model_id", None),
            "ai_semantic": bool(getattr(engine, "semantic", False)),
            "new_files_folder": str(getattr(cfg, "new_files_folder", "") or "~/Pictures/Ninaivu"),
            "ffmpeg": bool(media.FFMPEG),
        },
        "measured_at": time.time(),
    }
    from . import tiers                                          # noqa: PLC0415
    facts["tier"] = tiers.current(cfg, engine)
    facts["recommendations"] = recommend(facts)
    return facts


# ---------------------------------------------------------------------------
# Advice
# ---------------------------------------------------------------------------

def _size(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value / GB:.0f} GB" if value >= 10 * GB else f"{value / GB:.1f} GB"


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def _advice(key: str, level: str, title: str, detail: str,
            action: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": key, "level": level, "title": title, "detail": detail, "action": action}


def _connection(drive: dict[str, Any]) -> str:
    """"an internal solid-state drive", "a USB drive" — with its article."""
    kind = {True: "solid-state", False: "spinning"}.get(drive.get("solid_state"), "")
    where = "internal" if drive.get("internal") else (drive.get("bus") or "external")
    words = " ".join(x for x in (where, kind, "drive") if x)
    return ("an " if words[0].lower() in "aeio" else "a ") + words


def recommend(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """What would help, most pressing first. A plain function of ``facts``."""
    out: list[dict[str, Any]] = []
    mac = facts.get("platform") == "darwin"
    ninaivu = facts.get("ninaivu", {})
    gpu = facts.get("graphics", {})
    load_now = facts.get("load", {}) or {}
    backlog_now = facts.get("backlog", {}) or {}

    # -- the graphics processor ---------------------------------------------
    if gpu.get("in_use"):
        out.append(_advice(
            "gpu", "good", "The image model runs on the graphics processor",
            f"Photographs are analysed and searched on {gpu.get('name') or 'the GPU'}, "
            "which leaves the processor free for the gallery."))
    elif gpu.get("available") and ninaivu.get("ai_enabled") and ninaivu.get("ai_semantic"):
        name = gpu.get("name") or "a graphics processor"
        why = ("On an Apple M6, analysing photographs on it was twice as fast as on "
               "the processor, with identical results, and it leaves the processor "
               "free for the gallery.")
        if not ninaivu.get("ai_gpu"):
            out.append(_advice(
                "gpu", "consider", "Use the graphics processor for the image model",
                f"This computer has {name}, and Ninaivu is set to leave it idle. {why} "
                "Ninaivu restarts to make the change.",
                {"kind": "setting", "key": "ai_gpu", "value": True, "restart": True,
                 "label": "Use it and restart"}))
        else:
            out.append(_advice(
                "gpu", "consider", "Restart to put the image model on the graphics processor",
                f"This computer has {name} and Ninaivu may use it, but the model was loaded "
                f"on the processor. {why} If it is still on the processor after a restart, "
                "the graphics processor did not pass Ninaivu's check, and the log says why.",
                {"kind": "restart", "label": "Restart now"}))

    # -- the drives ------------------------------------------------------------
    for drive in facts.get("storage", []):
        path, role = drive.get("path", ""), drive.get("role")
        if not drive.get("present"):
            if role == "library":
                out.append(_advice(
                    f"missing:{path}", "act", "A library folder is not connected",
                    f"{path} is not there. Its photographs are still listed, but cannot be "
                    "opened, scanned or backed up until the drive is connected."))
            continue
        filesystem = (drive.get("filesystem") or "").lower()
        if role == "library" and drive.get("read_only"):
            elsewhere = (f"Edited copies, approved uploads and phone backups are saved to "
                         f"{ninaivu.get('new_files_folder') or '~/Pictures/Ninaivu'} instead, "
                         "which is in the library too; deleting, date corrections, turning "
                         "originals and archive moves cannot be done here.")
            if mac and filesystem == "ntfs":
                detail = ("The drive is formatted for Windows (NTFS), which macOS reads but "
                          f"does not write. {elsewhere} To write to it, copy the library to a "
                          "drive formatted APFS (Mac only) or exFAT (Mac and Windows) and "
                          "point Ninaivu at the copy, or install an NTFS driver for macOS.")
            else:
                detail = f"The drive is mounted read-only. {elsewhere}"
            out.append(_advice(f"read-only:{path}", "act",
                               "Ninaivu can read this library but not change it",
                               f"{path}: {detail}"))
        if role == "index":
            if drive.get("internal") is False or drive.get("solid_state") is False:
                out.append(_advice(
                    "index-drive", "consider", "Keep Ninaivu's index on the internal drive",
                    f"The index and thumbnails are on {_connection(drive)}. Every page of "
                    "the gallery reads them, and an internal solid-state drive answers "
                    "those reads many times faster."))
            elif drive.get("internal") and drive.get("solid_state"):
                out.append(_advice(
                    "index-drive", "good", "The index is on the internal solid-state drive",
                    "Thumbnails and the index — what every page of the gallery reads — "
                    "are on the fastest drive in the computer."))
            free, total = drive.get("free_bytes"), drive.get("total_bytes")
            if free is not None and (free < 5 * GB or (total and free / total < 0.05)):
                out.append(_advice(
                    "index-space", "act", "The drive holding the index is nearly full",
                    f"{_size(free)} free. New thumbnails and the index's own growth need "
                    "room; below a few gigabytes, scans and uploads start to fail."))

    # -- how long a scan takes -------------------------------------------------
    for root, scan in (facts.get("scans") or {}).items():
        if not scan or scan["seconds"] < 60:
            continue
        drive = next((d for d in facts.get("storage", []) if d.get("path") == root), {})
        rate = scan.get("files_per_second")
        out.append(_advice(
            f"scan:{root}", "consider", "Scans spend minutes reading the library drive",
            f"A scan that found nothing new took {_duration(scan['seconds'])} to look at "
            f"{scan['files']:,} files ({rate:,} a second), on {_connection(drive)}"
            + (f" formatted {drive['filesystem'].upper()}" if drive.get("filesystem") else "")
            + ". That time is the drive, not the processor. Connecting it straight to the "
            "computer rather than through a hub helps; a solid-state drive would be many "
            "times faster."))

    # -- memory, battery and the resource mode ---------------------------------
    memory_percent = load_now.get("memory_percent")
    swap = load_now.get("swap_used_bytes") or 0
    total = facts.get("machine", {}).get("memory_bytes") or 0
    if memory_percent is not None:
        if memory_percent >= 90 or (total and swap > 0.25 * total):
            out.append(_advice(
                "memory", "act", "This computer is short of memory",
                f"{memory_percent:.0f}% in use and {_size(swap)} swapped to disk. Ninaivu "
                f"itself uses {_size(load_now.get('ninaivu_memory_bytes'))}. Closing other "
                "apps, or the Power-saving mode, would stop the computer slowing down.",
                {"kind": "tab", "tab": "server", "label": "Open Server"}))
        else:
            out.append(_advice(
                "memory", "good", "Enough memory",
                f"{_size(load_now.get('memory_available_bytes'))} available; Ninaivu uses "
                f"{_size(load_now.get('ninaivu_memory_bytes'))}."))

    battery = load_now.get("battery")
    waiting = int(backlog_now.get("analysis", 0)) + int(backlog_now.get("faces", 0))
    if battery and not battery.get("plugged") and ninaivu.get("mode") == "performance":
        out.append(_advice(
            "battery", "consider", "Performance mode on battery",
            "The computer is running on its battery with every core given to Ninaivu. "
            "Standard or Power-saving would last much longer.",
            {"kind": "tab", "tab": "server", "label": "Change mode"}))
    elif ninaivu.get("mode") == "power-saving" and waiting >= 1000 and not (
            battery and not battery.get("plugged")):
        out.append(_advice(
            "mode", "consider", "A faster resource mode would clear the backlog sooner",
            f"{waiting:,} items are waiting to be analysed, and Power-saving gives Ninaivu "
            "one worker. The computer is on mains power; Standard or Performance would "
            "finish them far sooner.",
            {"kind": "tab", "tab": "server", "label": "Change mode"}))
    logical = facts.get("machine", {}).get("logical_cores") or 0
    if logical and ninaivu.get("workers", 0) > logical:
        out.append(_advice(
            "workers", "consider", "More workers than the processor has cores",
            f"Ninaivu is set to {ninaivu['workers']} workers on {logical} cores; the extra "
            "ones only take turns. The resource mode sets this from the computer.",
            {"kind": "tab", "tab": "server", "label": "Open Server"}))

    # -- waiting work and the schedule ---------------------------------------------
    if ninaivu.get("schedule") == "overnight" and waiting:
        start, end = (ninaivu.get("night") or ["23:00", "06:00"])[:2]
        out.append(_advice(
            "overnight", "consider", "Analysis is saved for the night",
            f"{waiting:,} items wait for {start}–{end} to be analysed, because background "
            "work is set to run overnight. New files still appear straight away. "
            "Balanced would analyse them while nobody is watching a video.",
            {"kind": "tab", "tab": "activity", "label": "Open Activity"}))

    # -- what is missing -------------------------------------------------------------
    if not ninaivu.get("ffmpeg"):
        out.append(_advice(
            "ffmpeg", "act", "ffmpeg is not installed",
            "Without it videos cannot be converted for the browser or given a poster "
            "frame, and sound files get pictures without the shape of their sound.",
            {"kind": "tab", "tab": "extras", "label": "Open Extras"}))
    if ninaivu.get("ai_enabled") and not ninaivu.get("ai_semantic"):
        out.append(_advice(
            "ai-model", "act", "Search by description is off",
            "The image model could not be loaded, so photographs are tagged by colour "
            "and shape only. The server log says why.",
            {"kind": "tab", "tab": "ai-models", "label": "Open AI models"}))

    order = {level: index for index, level in enumerate(LEVELS)}
    return sorted(out, key=lambda item: order[item["level"]])
