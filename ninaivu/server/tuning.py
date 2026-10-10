"""How hard Ninaivu works this computer: tuned to the machine it runs on.

The same Ninaivu runs on a Raspberry Pi with four small cores and a USB disk,
on a Mac mini with ten fast ones, and on a desktop with a graphics card. One
set of numbers cannot suit all three: eight indexing threads starve a Pi and
leave the Mac half idle. So at start Ninaivu measures the machine — its
cores, its memory, whether it is a single-board computer, whether there is a
graphics processor, and whether the library sits on a spinning disk — picks a
**profile** from that, and sizes every knob below from the profile.

The profiles:

``small``
    A single-board computer, or anything with under 6 GB of memory or two
    cores. Leaves most of the machine to the system and the household.
``medium``
    An everyday computer. About half the machine for background work.
``large``
    Eight cores or more with 16 GB, or a graphics processor with 16 GB. Most
    of the machine: 80 % of the cores, and at least two kept back for
    everything else. No fixed maximum: it used to stop at twelve, so an
    eighteen-core machine left six idle.
``peak``
    Never chosen by itself: the administrator asks for it (or the desktop
    panel's Performance mode). Up to 95 % of the processor and of the memory,
    and the rest left for the operating system — the ceiling, never more.

Cores are counted as the operating system counts them: every logical core,
of every kind. An Apple chip has two or three kinds (Super, Performance,
Efficiency); all of them are counted, because indexing and the analysis are
background work that macOS puts on any kind, and the slower kinds still
finish their share. A Mac with 18 cores is planned as 18.

The orientation survey can run beside the scan (straighten.py) on a machine
with a graphics processor and a solid-state library. Its readers are part of
the plan: the scan's workers and the survey's readers together stay within
the profile's share of the cores. Peak keeps :data:`SURVEY_BESIDE` cores for
it; a profile with no room left holds the scan while the survey runs, as it
did before the survey could run beside it.

Every knob can be set outright on the console's Tuning page, and set back to
automatic there. A value given at start (``--workers``, ``NINAIVU_SERVER_THREADS``,
a number written into config.json by hand) is kept and shown as such.

Two halves, as in capacity.py: :func:`plan` is a plain function of the facts,
so each rule is tested on every system; :func:`apply` puts a plan into the
running server.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from typing import Any

from ..words import said

log = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2

PROFILES = ("small", "medium", "large", "peak")

PROFILE_WORDS: dict[str, dict[str, str]] = {
    "small": {"label": said("Small box"),
              "description": said("A Raspberry Pi or a small always-on computer. Background work takes a little of the machine and leaves the rest for the system.")},
    "medium": {"label": said("Everyday computer"),
               "description": said("A laptop or an ordinary desktop. Background work takes about half the machine.")},
    "large": {"label": said("Powerful computer"),
              "description": said("Many cores and plenty of memory. Background work takes most of the machine and keeps two cores free.")},
    "peak": {"label": said("Peak performance"),
             "description": said("Up to 95% of the processor and memory for Ninaivu; the rest is left for the operating system.")},
}

#: The share of the machine each profile may plan to use, at most.
CEILING = {"small": 0.5, "medium": 0.6, "large": 0.8, "peak": 0.95}

#: When a change takes effect.
NOW, NEXT_SCAN, RESTART = "now", "next-scan", "restart"

#: Every knob: its bounds, its unit, and when a change takes effect.
KNOBS: dict[str, dict[str, Any]] = {
    "workers": {
        "label": said("Indexing workers"),
        "help": said("Photographs read and given thumbnails at the same time during a scan."),
        "min": 1, "max": 64, "unit": "", "applies": NEXT_SCAN},
    "compute_threads": {
        "label": said("Analysis threads"),
        "help": said("Processor threads the image model, video conversion and the editing models use."),
        "min": 1, "max": 64, "unit": "", "applies": NOW},
    "server_threads": {
        "label": said("Web threads"),
        "help": said("Pages, photographs and videos served at the same time to the family app and the console."),
        "min": 2, "max": 64, "unit": "", "applies": RESTART},
    "clip_batch_size": {
        "label": said("Image model batch"),
        "help": said("Photographs handed to the image model at once. Larger is faster and uses more memory."),
        "min": 1, "max": 256, "unit": "", "applies": NOW},
    "video_keyframes": {
        "label": said("Moments described per video"),
        "help": said("How many moments of each video the image model looks at. More finds more, and takes longer."),
        "min": 0, "max": 5, "unit": "", "applies": NOW},
    "cloud_parallel": {
        "label": said("Backup uploads at once"),
        "help": said("Files Mugil sends to the cloud at the same time."),
        "min": 1, "max": 6, "unit": "", "applies": NOW},
    "db_cache_mb": {
        "label": said("Index cache"),
        "help": said("Memory each connection to the index keeps of it, so that pages are answered without reading the disk."),
        "min": 2, "max": 512, "unit": "MB", "applies": RESTART},
}

#: Knobs that are a setting in Config of the same name.
CONFIG_KNOBS = ("workers", "clip_batch_size", "video_keyframes", "cloud_parallel")

#: Readers of a spinning disk beyond this only make its head jump between them.
SPINNING_READERS = 4

#: Cores Peak keeps for the orientation survey running beside the scan: two,
#: the fewest it runs beside the scan with (straighten.room_beside_the_scan).
SURVEY_BESIDE = 2

#: The most photographs the survey judges at once beside the scan.
MAX_SURVEY_BESIDE = 8

#: Rough memory each part takes, for the estimate the Tuning page shows. Measured
#: on the fixtures and a Mac mini; an estimate, and said to be one.
BASE_BYTES = 350 * MB
MODEL_BYTES = {"full": 1200 * MB, "basic": 150 * MB}
PER_WORKER_BYTES = 160 * MB          # a 24-megapixel photograph decoded, and its resizes
PER_COMPUTE_BYTES = 40 * MB
PER_BATCH_ITEM_BYTES = {"gpu": 12 * MB, "cpu": 30 * MB}
PER_SERVER_THREAD_BYTES = 8 * MB


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


# ---------------------------------------------------------------------------
# The machine and its profile
# ---------------------------------------------------------------------------

def facts(cfg: Any, engine: Any = None) -> dict[str, Any]:
    """What the plan is made from, measured on this computer."""
    from . import capacity, tiers                                # noqa: PLC0415
    from ..media import components                               # noqa: PLC0415

    parts = capacity.machine()
    gpu = capacity.graphics(engine).get("available")
    spinning = None
    for root in list(getattr(cfg, "roots", []) or [])[:4]:
        drive = capacity.storage(root, "library")
        if drive.get("solid_state") is False:
            spinning = True
            break
        if drive.get("solid_state") is True and spinning is None:
            spinning = False
    tier = tiers.chosen(cfg) or tiers.detect(
        parts.get("memory_bytes"), gpu, bool(parts.get("apple_silicon")),
        tiers._model_installed())
    return {
        "cores": int(parts.get("logical_cores") or os.cpu_count() or 1),
        "memory_bytes": parts.get("memory_bytes"),
        "apple_silicon": bool(parts.get("apple_silicon")),
        "board": parts.get("board"),
        "gpu": gpu if gpu in ("cuda", "mps") else None,
        "library_spinning": spinning,
        "tier": tier,
        "ai_enabled": bool(getattr(cfg, "ai_enabled", False)),
        "ffmpeg": components.ffmpeg_available(),
        "mode": os.environ.get("NINAIVU_RESOURCE_MODE") or None,
    }


def detect(machine: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """The profile a machine measures as, and why: a ``said`` template and its values."""
    cores = int(machine.get("cores") or 1)
    memory = machine.get("memory_bytes")
    gb = None if memory is None else memory / GB
    shown = {"cores": cores, "gb": "?" if gb is None else f"{gb:.0f}"}
    if machine.get("board"):
        return "small", said("{board}: a single-board computer"), {"board": machine["board"], **shown}
    if gb is not None and gb < 6:
        return "small", said("{gb} GB of memory"), shown
    if cores <= 2:
        return "small", said("{cores} cores"), shown
    if gb is not None and gb >= 16 and (cores >= 8 or machine.get("gpu")):
        return "large", said("{cores} cores and {gb} GB of memory"), shown
    return "medium", said("{cores} cores and {gb} GB of memory"), shown


def chosen_profile(cfg: Any, machine: dict[str, Any]) -> tuple[str, str]:
    """The profile in use, and where it came from: ``set`` on the Tuning page,
    ``mode`` from the desktop panel's resource mode, or ``measured``."""
    told = str(getattr(cfg, "tuning_profile", "auto") or "auto").strip().lower()
    if told in PROFILES:
        return told, "set"
    mode = machine.get("mode")
    if mode == "performance":
        return "peak", "mode"
    if mode == "power-saving":
        return "small", "mode"
    return detect(machine)[0], "measured"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def _automatic(profile: str, machine: dict[str, Any]) -> dict[str, int]:
    cores = int(machine.get("cores") or 1)
    gpu = bool(machine.get("gpu") or machine.get("apple_silicon"))
    if profile == "small":
        values = {"workers": _clamp(cores // 2, 1, 2), "compute_threads": _clamp(cores // 2, 1, 2),
                  "server_threads": 6, "clip_batch_size": 4, "cloud_parallel": 2, "db_cache_mb": 8}
    elif profile == "medium":
        values = {"workers": _clamp(cores // 2, 2, 6), "compute_threads": _clamp(cores // 2, 2, 6),
                  "server_threads": 8, "clip_batch_size": 16 if gpu else 8, "cloud_parallel": 3,
                  "db_cache_mb": 16}
    elif profile == "large":
        # 80 % of the cores with at least two kept back: 8 of 10, 14 of 18,
        # 19 of 24, and 2 of 4 (a four-core machine with a graphics processor
        # and 16 GB measures as large; a floor of four used all of it). There was a fixed maximum of twelve here, which left an
        # eighteen-core Mac a third idle in Balanced.
        share = min(cores - 2, math.floor(cores * CEILING["large"]))
        values = {"workers": _clamp(share, 1, 64), "compute_threads": _clamp(share, 1, 64),
                  "server_threads": _clamp(cores, 12, 24), "clip_batch_size": 32 if gpu else 16,
                  "cloud_parallel": 4, "db_cache_mb": 32}
    else:
        # 95 % of the cores, rounded down so the ceiling is never crossed:
        # 19 of 20, 9 of 10, 3 of 4.
        share = max(1, math.floor(cores * CEILING["peak"]))
        values = {"workers": share, "compute_threads": share,
                  "server_threads": _clamp(cores * 2, 8, 32),
                  "clip_batch_size": 64 if gpu else 32, "cloud_parallel": 6, "db_cache_mb": 128}
        if _survey_may_run_beside(machine) and share >= 3 * SURVEY_BESIDE:
            # The survey runs beside the scan here; its readers come out of
            # the same 95 %, so the scan keeps the rest: 15 + 2 of 18 cores.
            values["workers"] = share - SURVEY_BESIDE
    # How many moments of a video to describe is the tier's call (tiers.py):
    # it decides what is found, not only how fast.
    if machine.get("tier") == "basic":
        values["video_keyframes"] = 1
        values["clip_batch_size"] = min(values["clip_batch_size"], 4)
    else:
        values["video_keyframes"] = 5
    if machine.get("mode") == "power-saving" and profile == "small":
        values["workers"], values["compute_threads"] = 1, min(2, values["compute_threads"])
    return values


def _survey_may_run_beside(machine: dict[str, Any]) -> bool:
    """Whether the orientation survey can run beside the scan on this machine:
    a graphics processor, and a library known to be on a solid-state disk
    (what straighten.room_beside_the_scan asks of the running server)."""
    return bool(machine.get("gpu") or machine.get("apple_silicon")) and \
        machine.get("library_spinning") is False


def survey_beside(values: dict[str, int], machine: dict[str, Any], profile: str) -> int:
    """Photographs the survey judges at once beside the scan: what the scan's
    workers leave of the profile's share of the cores. Under two, it does not
    run beside the scan at all (0): it holds the scan while it looks."""
    if not _survey_may_run_beside(machine):
        return 0
    cores = int(machine.get("cores") or 1)
    room = math.floor(cores * CEILING[profile]) - int(values["workers"])
    return min(room, MAX_SURVEY_BESIDE) if room >= 2 else 0


def estimate(values: dict[str, int], machine: dict[str, Any],
             survey: int = 0) -> dict[str, Any]:
    """What these numbers are expected to use of the machine, at most: while
    a scan and the analysis are both running, with every cache full, and the
    orientation survey beside them when *survey* readers may run there."""
    cores = int(machine.get("cores") or 1)
    busy = max(int(values["workers"]) + survey, int(values["compute_threads"]))
    on = "gpu" if (machine.get("gpu") or machine.get("apple_silicon")) else "cpu"
    model = MODEL_BYTES.get(machine.get("tier") or "basic", 0) if machine.get("ai_enabled") else 0
    connections = int(values["server_threads"]) + int(values["workers"]) + 2
    memory = (BASE_BYTES + model
              + (int(values["workers"]) + survey) * PER_WORKER_BYTES
              + int(values["compute_threads"]) * PER_COMPUTE_BYTES
              + int(values["clip_batch_size"]) * PER_BATCH_ITEM_BYTES[on] * (1 if machine.get("ai_enabled") else 0)
              + int(values["server_threads"]) * PER_SERVER_THREAD_BYTES
              + connections * int(values["db_cache_mb"]) * MB)
    total = machine.get("memory_bytes")
    return {
        "cores": min(busy, cores), "of_cores": cores,
        "cpu_percent": round(100 * min(busy, cores) / cores),
        "memory_bytes": int(memory), "of_memory": total,
        "memory_percent": round(100 * memory / total) if total else None,
        "disk_readers": int(values["workers"]) + survey,
        "uploads": int(values["cloud_parallel"]),
    }


def _fit(values: dict[str, int], machine: dict[str, Any], profile: str) -> list[str]:
    """Shrink what is automatic until the estimate is under the profile's
    share of memory: the batch first, then the cache, then the workers."""
    total = machine.get("memory_bytes")
    ceiling = CEILING[profile]
    shrunk: list[str] = []
    if not total:
        return shrunk
    while estimate(values, machine, survey_beside(values, machine, profile))["memory_bytes"] \
            > ceiling * total:
        if values["clip_batch_size"] > 4:
            values["clip_batch_size"] //= 2
            name = "clip_batch_size"
        elif values["db_cache_mb"] > 4:
            values["db_cache_mb"] //= 2
            name = "db_cache_mb"
        elif values["workers"] > 1:
            values["workers"] -= 1
            name = "workers"
        elif values["compute_threads"] > 1:
            values["compute_threads"] -= 1
            name = "compute_threads"
        else:
            break
        if name not in shrunk:
            shrunk.append(name)
    return shrunk


def plan(machine: dict[str, Any], profile: str, overrides: dict[str, Any] | None = None,
         startup: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every knob's value for this machine and profile, where each came from,
    and what the whole is expected to use. A plain function of its arguments.

    *overrides* are the administrator's (the Tuning page), *startup* the
    values the start command or the settings file gave; an override wins over
    a start-up value, which wins over the automatic one.
    """
    overrides, startup = dict(overrides or {}), dict(startup or {})
    auto = _automatic(profile, machine)
    notes: dict[str, tuple[str, dict[str, Any]]] = {}
    if machine.get("library_spinning") and auto["workers"] > SPINNING_READERS:
        auto["workers"] = SPINNING_READERS
        notes["workers"] = (said("at most {count}: the library is on a spinning disk, and more readers only make it slower"),
                            {"count": SPINNING_READERS})
    for name in _fit(auto, machine, profile):
        notes.setdefault(name, (said("lowered to stay within {percent}% of the memory"),
                                {"percent": round(100 * CEILING[profile])}))

    values, knobs = {}, []
    for name, knob in KNOBS.items():
        source = "auto"
        value = auto[name]
        if name in startup and startup[name] is not None:
            value, source = int(startup[name]), "startup"
        if name in overrides and overrides[name] is not None:
            value, source = int(overrides[name]), "set"
        value = _clamp(value, knob["min"], knob["max"])
        values[name] = value
        note, params = notes.get(name, (None, None))
        knobs.append({"name": name, "label": knob["label"], "help": knob["help"],
                      "min": knob["min"], "max": knob["max"], "unit": knob["unit"],
                      "applies": knob["applies"], "value": value, "auto": auto[name],
                      "source": source, "note": note, "note_params": params})
    survey = survey_beside(values, machine, profile)
    usage = estimate(values, machine, survey)
    usage["survey_beside"] = survey
    ceiling = CEILING[profile]
    usage["ceiling_percent"] = round(100 * ceiling)
    usage["over"] = bool(usage["cpu_percent"] > 100 * ceiling + 0.5 or (
        usage["memory_percent"] is not None and usage["memory_percent"] > 100 * ceiling + 0.5))
    return {"profile": profile, "values": values, "knobs": knobs, "usage": usage,
            "survey_beside": survey}


def clean_overrides(raw: Any) -> dict[str, int]:
    """The overrides kept in config.json, with anything unknown or out of
    bounds dropped — a hand-edited file must not start 500 threads."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        knob = KNOBS.get(name)
        if knob is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        out[name] = _clamp(number, knob["min"], knob["max"])
    return out


# ---------------------------------------------------------------------------
# Putting it into the running server
# ---------------------------------------------------------------------------

def _startup_values(cfg: Any) -> dict[str, Any]:
    """What the start command, the environment or config.json named outright,
    taken once at the first apply and kept for every later one."""
    from ..utils.resources import budget                         # noqa: PLC0415
    from .config import Config                                   # noqa: PLC0415

    kept = getattr(cfg, "_tuning_startup", None)
    if kept is not None:
        return kept
    shipped = Config()
    chosen = getattr(cfg, "_chosen", set()) or set()
    seeded = getattr(cfg, "_env_seeded", {}) or {}
    tier_filled = getattr(cfg, "_tier_filled", {}) or {}
    out: dict[str, Any] = {}
    for name in CONFIG_KNOBS:
        value = getattr(cfg, name, None)
        if name in chosen:
            out[name] = value
        elif name == "workers":
            # The desktop panel starts the server with --workers from its
            # resource mode; that is the mode speaking, not a number someone
            # picked, and the mode already chose the profile.
            from_mode = value == budget()["workers"] and os.environ.get("NINAIVU_RESOURCE_MODE")
            if not from_mode and (name in seeded or value != shipped.workers):
                out[name] = value
        elif name not in tier_filled and value != getattr(shipped, name):
            out[name] = value
    if os.environ.get("NINAIVU_SERVER_THREADS"):
        out["server_threads"] = cfg.server_threads
    if os.environ.get("NINAIVU_COMPUTE_THREADS", "").strip().isdigit():
        out["compute_threads"] = int(os.environ["NINAIVU_COMPUTE_THREADS"])
    cfg._tuning_startup = out
    return out


def current(cfg: Any, engine: Any = None, machine: dict[str, Any] | None = None) -> dict[str, Any]:
    """The plan for this computer as it stands, with the facts and profile behind it."""
    machine = machine or facts(cfg, engine)
    profile, how = chosen_profile(cfg, machine)
    measured, why, why_params = detect(machine)
    result = plan(machine, profile, clean_overrides(getattr(cfg, "tuning", {})),
                  _startup_values(cfg))
    from ..words import filled                                   # noqa: PLC0415
    result.update({
        "profile_source": how, "measured": measured,
        "why": filled(why, why_params), "why_key": why, "why_params": why_params,
        "setting": str(getattr(cfg, "tuning_profile", "auto") or "auto"),
        "profiles": [{"id": p, **PROFILE_WORDS[p], "ceiling_percent": round(100 * CEILING[p])}
                     for p in PROFILES],
        "machine": machine,
    })
    return result


def apply(cfg: Any, engine: Any = None, services: Any = None,
          machine: dict[str, Any] | None = None) -> dict[str, Any]:
    """Put the plan into the running server. Settings that live in Config are
    set there and marked as filled in (so config.json is not given them: the
    choice stays automatic, and :func:`plan` decides again next start); the
    rest go to the parts that read them. Returns the plan."""
    from ..storage import db                                     # noqa: PLC0415
    from ..utils import resources                                # noqa: PLC0415

    result = current(cfg, engine, machine)
    values = result["values"]
    filled = dict(getattr(cfg, "_tier_filled", {}) or {})
    startup = _startup_values(cfg)
    for name in CONFIG_KNOBS:
        # The plan has already held every number to its knob's bounds. A
        # number the household chose (config.json, the environment, the start
        # command) still stands — but within those bounds too: left as it was
        # given, "cloud_parallel": 500 started 500 upload threads while the
        # Tuning page showed 6.
        setattr(cfg, name, values[name])
        seeded = getattr(cfg, "_env_seeded", None)
        if isinstance(seeded, dict) and name in seeded:
            seeded[name] = values[name]         # so save() does not take it for a change
        if name in startup and name not in (getattr(cfg, "tuning", {}) or {}):
            continue                            # the household's own number stands
        filled[name] = values[name]
    cfg._tier_filled = filled
    # Likewise: NINAIVU_SERVER_THREADS=0 gave a web server with no threads,
    # which took every connection and answered none.
    cfg.server_threads = values["server_threads"]
    if getattr(cfg, "_tuning_running", None) is None:
        # What this process started with, for what only a restart changes.
        cfg._tuning_running = {name: values[name] for name, knob in KNOBS.items()
                               if knob["applies"] == RESTART}
    # For straighten.readers_for: how many the survey judges at once beside
    # the scan, within the plan (0: no room, it holds the scan instead).
    cfg._survey_beside = result["survey_beside"]
    resources.tune(compute_threads=values["compute_threads"])
    db.set_cache_mb(values["db_cache_mb"])
    _threads_now(values["compute_threads"])
    cloud = getattr(services, "cloud", None)
    if cloud is not None:
        try:
            cloud.apply_settings()
        except Exception:                                        # noqa: BLE001
            log.debug("the backup did not take the new settings", exc_info=True)
    result["restart_needed"] = restart_needed(cfg, values)
    log.info("tuning: %s profile (%s) — %s", result["profile"], result["profile_source"],
             ", ".join(f"{k} {v}" for k, v in values.items()))
    return result


def restart_needed(cfg: Any, values: dict[str, int]) -> list[str]:
    """The knobs whose new value waits for a restart."""
    running = getattr(cfg, "_tuning_running", None) or {}
    return [name for name, was in running.items() if values.get(name) != was]


def save(cfg: Any, profile: Any = None, values: Any = None, reset: bool = False) -> None:
    """Take what the Tuning page sent. *values* maps a knob to a number, or
    to None for "back to automatic". Raises ValueError with what is wrong."""
    overrides = clean_overrides(getattr(cfg, "tuning", {}))
    if reset:
        cfg.tuning_profile, cfg.tuning = "auto", {}
        return
    if profile is not None:
        profile = str(profile).strip().lower()
        if profile != "auto" and profile not in PROFILES:
            raise ValueError("Choose automatic, small, medium, large or peak.")
    if values is not None:
        if not isinstance(values, dict):
            raise ValueError("Send the values as a name and a number each.")
        for name, value in values.items():
            knob = KNOBS.get(name)
            if knob is None:
                raise ValueError(f"There is no setting called {name}.")
            if value is None:
                overrides.pop(name, None)
                continue
            if isinstance(value, bool):
                raise ValueError(f"{name} is a number.")
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name} is a number.") from None
            if not knob["min"] <= number <= knob["max"]:
                raise ValueError(f"{name} is between {knob['min']} and {knob['max']}.")
            overrides[name] = number
    if profile is not None:
        cfg.tuning_profile = profile
    cfg.tuning = overrides


def _threads_now(count: int) -> None:
    """Tell the numerical libraries how many threads they may use: through
    the environment for those not loaded yet, and directly for those that are."""
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                 "OPENCV_FOR_THREADS_NUM"):
        os.environ[name] = str(count)
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.set_num_threads(count)
        except Exception:                                        # noqa: BLE001
            log.debug("torch kept its thread count", exc_info=True)
