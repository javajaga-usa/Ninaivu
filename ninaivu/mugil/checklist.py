"""The Mugil checklist, ticked from what is actually set up.

The two-computer plan is a list of things that each have to be true: the two
are paired, the Secondary is up, every original is on it, there is a copy out
of the house, the Mac wakes for its night's work. A list a person ticks says
what was done once; this one looks, every time it is asked, and says what is
true now. Each item is one of:

* ``done``: true now;
* ``todo``: not set up yet, with what to do;
* ``problem``: was working and is not now (the Secondary has not answered for
  a day, its disk reports trouble);
* ``off``: not needed with the household's choices (no wake while the daily
  catch-up is off).

The items carry ids and numbers, and the console says them in the reader's
language. Nothing here changes anything, and with one computer there is no
list.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import Any, Callable

from .. import __version__
from . import PRIMARY, SECONDARY, pairing, role

DONE, TODO, PROBLEM, OFF = "done", "todo", "problem", "off"

DAY = 86400.0
#: Less free than this on a disk that keeps originals is a problem.
LOW_FREE = 0.05


def _item(item_id: str, status: str, **facts: Any) -> dict[str, Any]:
    return {"id": item_id, "status": status, **facts}


def _seen(seen_at: float | None, now: float, *, item_id: str, within: float) -> dict[str, Any]:
    if not seen_at:
        return _item(item_id, TODO)
    return _item(item_id, PROBLEM if now - seen_at > within else DONE, seen_at=seen_at)


def _disk(health: dict[str, Any], item_id: str) -> dict[str, Any]:
    if not health:
        return _item(item_id, TODO)
    smart = health.get("smart") or {}
    free, total = int(health.get("free_bytes") or 0), int(health.get("total_bytes") or 0)
    facts = {"free_bytes": free, "total_bytes": total, "smart": smart.get("passed")}
    if health.get("reachable") is False:
        return _item(item_id, PROBLEM, unreachable=True, **facts)
    if smart.get("passed") is False or (total and free / total < LOW_FREE):
        return _item(item_id, PROBLEM, **facts)
    return _item(item_id, DONE, **facts)


def network_wake(runner: Callable[..., Any] = subprocess.run,
                 platform: str | None = None) -> bool | None:
    """On a Mac: whether *Wake for network access* is on (``pmset -g``,
    ``womp 1``); None where it cannot be read."""
    if (platform or sys.platform) != "darwin" or not shutil.which("pmset"):
        return None
    try:
        done = runner(["pmset", "-g"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (done.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "womp":
            return parts[1] == "1"
    return None


def _sign_in() -> bool | None:
    if sys.platform not in ("darwin", "win32"):
        return None
    try:
        from ..desktop import autostart                            # noqa: PLC0415
        return bool(autostart.enabled())
    except Exception:                                             # noqa: BLE001 - nice to have
        return None


def _originals_confirmed(cfg, conn, courier) -> tuple[int, int]:
    """The originals in the library now, and how many of those very files the
    Secondary has confirmed. Not the two counts side by side: the record
    keeps files deleted since, and the index counts those in the bin, so
    the item was ticked while new photographs were still to be sent."""
    from pathlib import Path                                       # noqa: PLC0415
    total = confirmed = 0
    have_record = (Path(cfg.state_dir) / courier.SENT).exists()
    for root in cfg.roots:
        sent: dict[str, int] = {}
        if have_record:
            with courier.sent_record(cfg.state_dir) as record:
                sent = {r["rel_path"]: int(r["size"]) for r in record.execute(
                    "SELECT rel_path, size FROM sent WHERE root=?", (str(root),))}
        for rel, size in conn.execute("SELECT rel_path, size FROM assets "
                                      "WHERE root=? AND trashed=0", (str(root),)):
            total += 1
            if rel in sent and sent[rel] == int(size or 0):
                confirmed += 1
    return total, confirmed


def primary(cfg, conn, services, *, now: float | None = None,
            network_wake_on: Callable[[], bool | None] = network_wake,
            sign_in: Callable[[], bool | None] = _sign_in) -> list[dict[str, Any]]:
    from . import courier, machines, wake                          # noqa: PLC0415
    now = now or time.time()
    items: list[dict[str, Any]] = []
    paired = pairing.load(cfg.state_dir) is not None
    items.append(_item("paired", DONE if paired else TODO))
    if not paired:
        return items
    other = machines.remembered(cfg.state_dir) or {}
    items.append(_seen(other.get("seen_at"), now, item_id="secondary_seen", within=DAY))
    same = machines.same_version(__version__, other.get("version"))
    items.append(_item("same_version", TODO if same is None else DONE if same else PROBLEM,
                       mine=__version__, theirs=other.get("version")))
    total, confirmed = _originals_confirmed(cfg, conn, courier)
    items.append(_item("originals", DONE if total and confirmed >= total else TODO,
                       confirmed=min(confirmed, total), total=total))
    items.append(_disk(other.get("health") or {}, "secondary_disk"))
    offsite = getattr(services, "offsite", None)
    problem = None
    if getattr(cfg, "offsite_enabled", False) and offsite is not None:
        try:
            problem = offsite.problem()
        except Exception:                                         # noqa: BLE001 - said as a problem
            problem = "unknown"
    items.append(_item("offsite", TODO if not getattr(cfg, "offsite_enabled", False)
                       else PROBLEM if problem else DONE))
    maker = getattr(services, "mugil_display", None)
    if maker is not None:
        shown = maker.status()
        made, owed = int(shown.get("made") or 0), int(shown.get("owed") or 0)
        items.append(_item("display_copies", DONE if made and not owed else TODO,
                           made=made, owed=owed))
    catchup = bool(getattr(cfg, "daily_catchup", False))
    items.append(_item("catchup", DONE if catchup else TODO))
    told = wake.told(cfg.state_dir)
    womp = network_wake_on()
    if not catchup:
        items.append(_item("wake", OFF))
    elif womp is False:
        items.append(_item("wake", PROBLEM, network_wake=False))
    else:
        items.append(_item("wake", DONE if told.get("macs") and told.get("at") else TODO,
                           at=told.get("at") or wake.wanted_at(cfg)))
    starts = sign_in()
    items.append(_item("sign_in", OFF if starts is None else DONE if starts else TODO))
    return items


def secondary(cfg, *, now: float | None = None) -> list[dict[str, Any]]:
    from . import machines, wake                                   # noqa: PLC0415
    from .hold import Holding, folder_problem                      # noqa: PLC0415
    now = now or time.time()
    folder = str(getattr(cfg, "mugil_originals_dir", "") or "")
    problem = folder_problem(folder, list(cfg.roots), cfg.state_dir) if folder else "none"
    items = [_item("originals_folder", DONE if problem is None else TODO if not folder else PROBLEM)]
    paired = pairing.load(cfg.state_dir) is not None
    items.append(_item("paired", DONE if paired else TODO))
    if not paired:
        return items
    other = machines.remembered(cfg.state_dir) or {}
    items.append(_seen(other.get("seen_at"), now, item_id="primary_seen", within=2 * DAY))
    if problem is None:
        holding = Holding(folder)
        items.append(_disk(machines.keeping_health(holding), "originals_disk"))
    asked = wake.load(cfg.state_dir)
    items.append(_item("wake", OFF if asked.get("macs") and not asked.get("at")
                       else DONE if asked.get("macs") else TODO,
                       at=asked.get("at") or "", last_woken=asked.get("last_woken")))
    return items


def checklist(cfg, conn, services, **kwargs: Any) -> dict[str, Any]:
    part = role(cfg)
    if part == PRIMARY:
        items = primary(cfg, conn, services, **kwargs)
    elif part == SECONDARY:
        items = secondary(cfg, now=kwargs.get("now"))
    else:
        items = []
    done = sum(1 for i in items if i["status"] in (DONE, OFF))
    return {"role": part, "items": items, "done": done, "of": len(items)}


__all__ = ["checklist", "primary", "secondary", "network_wake", "DONE", "TODO", "PROBLEM", "OFF"]
