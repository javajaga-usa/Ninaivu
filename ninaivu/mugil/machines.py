"""The two computers, side by side: which is which, how each is, and whether
they run the same Ninaivu.

Each computer describes itself (:func:`this_machine`): its name and part,
its Ninaivu version, its system, and the free space where it keeps things.
The Secondary adds how its originals disk is (:func:`keeping_health`): free
space, how much it keeps, when it last kept a file, and the disk's own SMART
verdict where the system lets an ordinary program ask (``smartctl``; nothing
is run that writes to a disk or tests it).

The Primary asks for the Secondary's description in the same signed hello the
copying starts with, and remembers the last answer (``mugil-machines.json``),
so the console can say when the Secondary was last seen while it is away.
The Secondary remembers when the Primary last spoke to it, and which version
it said it runs.

Two computers on different versions still work together, but the household is
told, because the next stages may need both to understand the same things.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .. import __version__
from . import display_name, role

log = logging.getLogger(__name__)

#: The last description of the other computer, in the state folder.
REMEMBERED = "mugil-machines.json"

_lock = threading.Lock()


def system_name() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if sys.platform.startswith("win"):
        return f"Windows {platform.release()}".strip()
    try:
        info = platform.freedesktop_os_release()
        return info.get("PRETTY_NAME") or "Linux"
    except (OSError, AttributeError):
        return f"{platform.system()} {platform.release()}".strip()


def _free(path: str | Path) -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"free_bytes": usage.free, "total_bytes": usage.total}


def this_machine(cfg) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": display_name(cfg), "role": role(cfg), "version": __version__,
        "system": system_name(), "machine": platform.machine(),
        "cores": os.cpu_count() or 0, "host": socket.gethostname(),
    }
    try:
        import psutil                                              # noqa: PLC0415
        out["memory_bytes"] = int(psutil.virtual_memory().total)
        out["up_since"] = float(psutil.boot_time())
    except Exception:                                             # noqa: BLE001 - nice to have
        pass
    space = _free(cfg.state_dir)
    if space:
        out["state_disk"] = space
    return out


def smart_verdict(path: str | Path, runner=subprocess.run) -> dict[str, Any] | None:
    """The SMART verdict of the disk *path* is on, or None when it cannot be asked.

    Only ``smartctl -H``: a question the disk answers from what it already
    knows. Most systems need administrator rights for it; without them this
    is None, and the console says the disk's health cannot be seen."""
    smartctl = shutil.which("smartctl")
    if not smartctl or not sys.platform.startswith("linux"):
        return None
    try:
        found = runner(["df", "--output=source", str(path)], capture_output=True, text=True,
                       timeout=10, check=False)
        device = (found.stdout or "").strip().splitlines()[-1].strip()
        if not device.startswith("/dev/"):
            return None
        done = runner([smartctl, "-H", "-j", device], capture_output=True, text=True,
                      timeout=20, check=False)
        answer = json.loads(done.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    status = answer.get("smart_status") or {}
    if "passed" not in status:
        return None
    return {"passed": bool(status["passed"]), "device": device,
            "model": (answer.get("model_name") or answer.get("model_family") or "")[:120]}


def keeping_health(holding, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """On the Secondary: how the originals disk is."""
    out: dict[str, Any] = {"folder": str(holding.folder), "reachable": holding.folder.is_dir()}
    space = _free(holding.folder)
    if space:
        out.update(space)
    summary = summary if summary is not None else holding.summary()
    out["files"] = summary.get("files", 0)
    out["bytes"] = summary.get("bytes", 0)
    out["last_kept"] = holding.last_kept()
    out["smart"] = smart_verdict(holding.folder) if out["reachable"] else None
    return out


# -- what each remembers of the other ---------------------------------------------

def remember(state_dir: str | Path, other: dict[str, Any]) -> None:
    with _lock:
        path = Path(state_dir) / REMEMBERED
        data = {**other, "seen_at": time.time()}
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("Could not remember the other computer: %s", exc)


def remember_hello(state_dir: str | Path, hello: dict[str, Any]) -> None:
    """On the Primary: what the Secondary said of itself in its hello."""
    remember(state_dir, {"machine": hello.get("machine") or {}, "health": hello.get("health") or {},
                         "version": hello.get("version"),
                         "family_port": hello.get("family_port"),
                         "offsite": hello.get("offsite") or {},
                         "copy_mode": hello.get("copy_mode") or "automatic",
                         "manual": hello.get("manual") or {}})


#: On the Secondary: what the Primary said of itself at its last run.
ABOUT_PRIMARY = "mugil-primary.json"


def remember_primary(state_dir: str | Path, about: dict[str, Any]) -> None:
    with _lock:
        path = Path(state_dir) / ABOUT_PRIMARY
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({**about, "told_at": time.time()}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("Could not remember what the Primary said: %s", exc)


def about_primary(state_dir: str | Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / ABOUT_PRIMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def offsite_summary(offsite) -> dict[str, Any]:
    """What one computer says of the off-site copy it sends: on or off,
    when it last finished, how much of it is there, and whether something is
    wrong (never what: that stays on its own console)."""
    if offsite is None or not getattr(offsite, "enabled", False):
        return {"enabled": False}
    try:
        status = offsite.status()
    except Exception:                                             # noqa: BLE001 - said as a problem
        return {"enabled": True, "problem": True}
    return {"enabled": True, "last_run": float(status.get("last_run") or 0),
            "files": int(status.get("files") or 0), "kept": int(status.get("kept") or 0),
            "problem": bool(status.get("problem") or status.get("error"))}


def phone_inbox_url(cfg) -> str | None:
    """On a paired Primary: the always-on Secondary's phone inbox, where a
    sync app can send photographs while this computer sleeps."""
    from urllib.parse import urlsplit                              # noqa: PLC0415
    from . import PRIMARY, pairing                                 # noqa: PLC0415
    if role(cfg) != PRIMARY or pairing.load(cfg.state_dir) is None:
        return None
    other = remembered(cfg.state_dir) or {}
    try:
        port = int(other.get("family_port") or 0)
    except (TypeError, ValueError):
        return None
    parts = urlsplit(str(getattr(cfg, "mugil_partner_url", "") or ""))
    if not port or not parts.hostname:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    plain = (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
    return f"{parts.scheme}://{host}{'' if plain else f':{port}'}/dav/"


def remembered(state_dir: str | Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(state_dir) / REMEMBERED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def forget(state_dir: str | Path) -> None:
    with _lock:
        (Path(state_dir) / REMEMBERED).unlink(missing_ok=True)
        (Path(state_dir) / ABOUT_PRIMARY).unlink(missing_ok=True)


def same_version(mine: str, theirs: str | None) -> bool | None:
    """Whether the two run the same Ninaivu; None when the other's is not known."""
    if not theirs:
        return None
    return str(mine).strip() == str(theirs).strip()


__all__ = ["this_machine", "keeping_health", "smart_verdict", "remember", "remembered",
           "offsite_summary", "remember_primary", "about_primary",
           "forget", "same_version", "system_name"]
