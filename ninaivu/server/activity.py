"""Everything Ninaivu is doing right now, gathered into one honest list.

The status strip used to show the library scan and nothing else. Every other
long job — a consolidation walking a USB disk, a cloud upload reading and
encrypting a photograph at a time, a straightening pass running a model over
tens of thousands of pictures, a storage check hashing the whole library, a
model still downloading — ran with no sign of it anywhere except the one
console page that owns it. So the honest answer to "what is using my disk?"
was: open six pages and add them up, and know to look in the first place.

That is what this module is for. A household that can hear the fan spinning is
entitled to a name for it, on the screen they already have open.

Nothing here starts, stops or owns any work. It *asks* each subsystem what it
is doing, on every poll, so the list cannot drift out of step with the jobs it
describes the way a registry written to at the start and end of each job can.
Each source is wrapped on its own: a subsystem that raises costs its own line
in the strip, not the whole strip.

Every job reports the same few things:

``id``       a stable key, so the browser can keep a row in place
``title``    what it is, in a word or two
``detail``   what it is doing right now, in plain language and without an ETA
``percent``  0-100, or ``None`` when it genuinely cannot be known yet
``eta``      seconds left, or ``None``; the browser puts it into words
``paused``   it is running but not moving, and says so rather than looking hung
``page``     the console page that can look at it properly, or stop it
``uses``     ``disk``, ``cpu``, ``network`` — what this one is actually spending
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..words import said

log = logging.getLogger(__name__)

DISK = "disk"
CPU = "cpu"
NETWORK = "network"

#: What each scan phase is called where there is room for a word.
SCAN_PHASES = {
    "walking": said("Looking for files"),
    "indexing": said("Indexing"),
    "tagging": said("Analysing with AI"),
    "videos": said("Describing videos"),
    "naming": said("Naming places"),
    "reading": said("Reading text in photos"),
    "faces": said("Finding faces"),
    "covers": said("Drawing pictures for sound files"),
    "finishing": said("Finishing up"),
    "paused": said("Indexing"),
}

#: The phases that are the AI model working rather than the disk being read.
#: Worth separating because they answer different questions: a phase spending
#: the CPU stays slow on a fast disk, and no amount of waiting for a drive
#: explains it.
THINKING_PHASES = {"tagging", "videos", "naming", "reading", "faces", "covers"}


def _count(done: int, total: int) -> str:
    """``1,204 of 90,300`` — or just the count, when no total is known yet."""
    return f"{done:,} of {total:,}" if total else f"{done:,}"


def _percent(done: int, total: int) -> int | None:
    """Never 100 while it is still running, and never a number without a total."""
    if not total:
        return None
    return max(0, min(99, int(done * 100 / total)))


def _job(id_: str, title: str, detail: str, *, page: str, uses: list[str],
         percent: int | None = None, eta: float | None = None,
         paused: bool = False) -> dict[str, Any]:
    return {
        "id": id_,
        "title": title,
        "detail": detail,
        "percent": percent,
        "eta": float(eta) if eta else None,
        "paused": bool(paused),
        "page": page,
        "uses": uses,
    }


# ---------------------------------------------------------------------------
# One function per kind of work. Each returns a job, a list of them, or None
# when that subsystem is doing nothing. None of them may raise: see `running`.
# ---------------------------------------------------------------------------

def _indexing(services: Any, config: Any) -> dict[str, Any] | None:
    scanner = getattr(services, "scanner", None)
    if scanner is None:
        return None
    scan = scanner.progress.snapshot()
    if not scan.get("running"):
        return None
    status = scan.get("status") or "indexing"
    held = str(scan.get("held") or "")
    paused = status == "paused" or bool(held)
    if held and status != "paused":
        # Making way for the household (ninaivu/server/workload.py) — said so,
        # or a scan waiting for a video to finish reads as one that has hung.
        detail = f"Waiting — {held}"
    elif paused:
        # Named, because "paused" on its own reads as something gone wrong.
        # What it means is that another job has the disk, and that job is a
        # line further up this same list.
        held_by = scanner.deferred
        detail = (f"Waiting for {held_by} to finish" if held_by
                  else scan.get("message") or "Waiting")
    elif scan.get("tag_total"):
        detail = _count(int(scan.get("tagged") or 0), int(scan["tag_total"]))
    else:
        detail = scan.get("message") or _count(
            int(scan.get("processed") or 0), int(scan.get("total") or 0))
    # Where the walk has got to. A count on its own cannot tell a scan working
    # steadily through a big folder from one waiting on a drive that has
    # stopped answering; a folder name that keeps changing can.
    if scan.get("folder") and not paused:
        detail = f"{detail} · {scan['folder']}"
    return _job(
        "indexing", SCAN_PHASES.get(status, "Indexing"), detail,
        page="library",
        uses=[CPU, DISK] if status in THINKING_PHASES else [DISK],
        # A walk has no total yet — the count it is heading for is what it is
        # finding — so it gets no percentage rather than a confident 0%.
        percent=None if paused or status == "walking" else scan.get("percent"),
        eta=scan.get("eta"), paused=paused,
    )


def _archive(services: Any, config: Any) -> dict[str, Any] | None:
    from ..archive import scanner as archive_scanner      # noqa: PLC0415

    if not archive_scanner.is_scanning():
        return None
    job = archive_scanner.job_progress()
    processed = int(job.get("processed") or 0)
    total = int(job.get("total_files") or 0)
    waiting = [str(drive) for drive in (job.get("waiting_for_drives") or [])]
    paused = bool(archive_scanner.is_scan_paused())
    if waiting:
        detail = f"Waiting for {', '.join(waiting)}"
    elif paused:
        detail = f"Paused at {_count(processed, total)}"
    else:
        detail = _count(processed, total)
        # Why it is going slowly, when it is: the pacing is deliberate and
        # looks identical from outside to a drive that has started to fail.
        if reason := (job.get("pacing") or {}).get("reason"):
            detail = f"{detail} · {reason}"
    mode = str(job.get("mode") or "").replace("_", " ") or "copying files"
    return _job(
        "archive", f"Archive — {mode}", detail,
        page="archive", uses=[DISK],
        percent=_percent(processed, total), eta=job.get("eta_seconds"),
        paused=paused or bool(waiting),
    )


def _cloud(services: Any, config: Any) -> dict[str, Any] | None:
    cloud = getattr(services, "cloud", None)
    engine = getattr(cloud, "_engine", None) if cloud is not None else None
    if engine is None:
        return None
    if not engine.running:
        # A backup that has stopped because Google will not accept the saved
        # permission any more is not "nothing running" — it is the most
        # important thing on the machine, and it stayed invisible here for
        # seven hours because the strip only showed what was moving.
        halted = engine.state.snapshot()
        if halted.get("needs_reconnect"):
            return _job(
                "cloud", said("Cloud backup has stopped"),
                said("Google is no longer accepting the saved permission — connect the account again on the Cloud page"),
                page="cloud", uses=[], paused=True)
        return None
    # The engine's own snapshot only. `CloudService.status()` also counts the
    # queue, and this is asked for every couple of seconds by every open tab.
    state = engine.state.snapshot()
    sent = int(state.get("current_sent") or 0)
    total = int(state.get("current_total") or 0)
    paused = bool(state.get("paused"))
    held = str(state.get("held_for") or "")
    away = [str(root) for root in (state.get("offline_roots") or [])]
    if away:
        # The drive holding those photographs is not plugged in. Named here
        # because the alternative — an upload that appears to be working while
        # sending nothing — is how a backup comes to look finished when it is
        # not; see the run loop in ninaivu/cloud/engine.py.
        detail = f"Waiting for {', '.join(away)} — not connected"
    elif paused:
        detail = said("Paused")
    elif held:
        detail = f"Waiting — {held}"
    elif name := str(state.get("current") or ""):
        speed = state.get("speed_formatted") or ""
        detail = f"{name} · {speed}" if speed else name
    else:
        detail = said("Looking for what has not gone up yet")

    # How far through the library, not how far through the file in flight.
    # The old bar was `current_sent / current_total`: it filled and emptied
    # every few seconds and read 90% with a hundred thousand photographs still
    # waiting. `queue_progress` keeps its answer for a few seconds, because
    # this is asked by every open tab every couple of seconds.
    done = whole = 0
    try:
        done, whole = cloud.queue_progress()
    except Exception:                                        # noqa: BLE001
        pass
    if whole and not away:
        detail = f"{done:,} of {whole:,} · {detail}"

    return _job(
        "cloud", said("Backing up to the cloud"), detail,
        page="cloud", uses=[NETWORK, DISK],
        percent=None if away else (_percent(done, whole) if whole
                                   else _percent(sent, total)),
        eta=None if away else state.get("eta_seconds"),
        paused=paused or bool(away) or bool(held),
    )


def _straighten(services: Any, config: Any) -> dict[str, Any] | None:
    straightener = getattr(services, "straightener", None)
    if straightener is None or not straightener.running:
        return None
    state = straightener.progress.snapshot()
    processed = int(state.get("processed") or 0)
    total = int(state.get("total") or 0)
    applying = state.get("status") == "applying"
    counted = _count(processed, total)
    phase = state.get("phase") or ""
    return _job(
        "straighten",
        said("Straightening photographs") if applying else said("Checking which way up"),
        f"{phase} · {counted}" if phase else counted,
        page="straighten", uses=[CPU, DISK],
        percent=_percent(processed, total),
    )


def _storage_check(services: Any, config: Any) -> dict[str, Any] | None:
    from ..api import admin_api                           # noqa: PLC0415

    state = dict(admin_api._SCRUBBER_PROGRESS)
    if not state.get("running"):
        return None
    processed = int(state.get("processed") or 0)
    total = int(state.get("total") or 0)
    held = str(state.get("held") or "")
    detail = _count(processed, total)
    return _job(
        "storage-check", said("Checking the library's storage"),
        f"{detail} · waiting — {held}" if held else detail,
        page="activity", uses=[DISK],
        percent=_percent(processed, total), paused=bool(held),
    )


def _downloads(services: Any, config: Any) -> list[dict[str, Any]]:
    from ..media import model_catalog                     # noqa: PLC0415

    jobs = []
    for model_id, state in model_catalog.downloads.active().items():
        done = int(state.get("done_bytes") or 0)
        total = int(state.get("total_bytes") or 0)
        name = model_catalog.MODELS.get(model_id, {}).get("label") or model_id
        jobs.append(_job(
            f"model:{model_id}", f"Downloading {name}",
            f"{done / 1048576:,.0f} MB of {total / 1048576:,.0f} MB" if total
            else f"{done / 1048576:,.0f} MB",
            page="ai-models", uses=[NETWORK],
            percent=_percent(done, total),
        ))
    return jobs


def _component_label(component_id: str) -> str:
    from ..media import components                        # noqa: PLC0415

    try:
        return str(components.catalogue_entry(component_id)["label"])
    except KeyError:
        return component_id


def _installs(services: Any, config: Any) -> list[dict[str, Any]]:
    from ..media import components                        # noqa: PLC0415

    jobs = []
    for component_id, state in components.installs.active().items():
        # An install has no total to measure against — the installer does not
        # say how much is left — so it shows its last line of output rather
        # than a bar that would have to be invented.
        last = next((line for line in reversed(state.get("log") or []) if line), "")
        jobs.append(_job(
            f"install:{component_id}",
            f"Installing {_component_label(component_id)}",
            last[:120], page="extras", uses=[NETWORK, DISK],
        ))
    return jobs


def _conversions(services: Any, config: Any) -> dict[str, Any] | None:
    """Videos being converted so a browser will play them.

    Two at a time, each one ffmpeg with the processor to itself — easily the
    heaviest thing Ninaivu does — and until now the only sign of it was on the
    page of whoever opened the video. Somebody starts a clip on their phone,
    gives up on it and locks the screen, and the machine goes on transcoding.

    The store is built on first use and kept in the app config, not on
    `Services`, so it may not exist yet; no conversion has ever run then.
    """
    store = (config or {}).get("MV_PROXIES") if config is not None else None
    if store is None:
        return None
    building = store.building()
    if not building:
        return None
    # One line however many are running: which video is being converted is
    # not the question, and two rows saying "Converting" is noise.
    first = building[0]
    detail = (f"{len(building)} videos" if len(building) > 1
              else first.message or "Converting")
    return _job(
        "conversions", said("Converting video for the browser"), detail,
        page="server", uses=[CPU, DISK],
        percent=first.percent if len(building) == 1 and first.percent else None,
    )


def _backup(services: Any, config: Any) -> dict[str, Any] | None:
    keeper = getattr(services, "backups", None)
    if keeper is None or not keeper.busy:
        return None
    return _job(
        "backup", said("Backing up Ninaivu's own records"),
        said("Making a bundle and checking that it restores"),
        page="activity", uses=[DISK],
    )


def _restore_test(services: Any, config: Any) -> dict[str, Any] | None:
    tester = getattr(services, "restore_tests", None)
    if tester is None or not tester.running:
        return None
    job = tester.current
    state = job.state.snapshot() if job is not None else {}
    done, total = int(state.get("processed") or 0), int(state.get("total") or 0)
    return _job(
        "restore-test", said("Testing a restore from Google Drive"),
        _count(done, total) if total else said("Choosing files to test"),
        page="cloud", uses=[NETWORK, DISK], percent=_percent(done, total),
    )


#: In the order they are shown. The jobs that hold the disk longest come
#: first, so the line most likely to be the answer is nearest the top.
SOURCES: list[Callable[[Any, Any], Any]] = [
    _archive, _indexing, _cloud, _conversions, _straighten, _storage_check,
    _downloads, _installs, _backup, _restore_test,
]


def other_work_active(services: Any, config: Any = None) -> bool:
    """Whether cloud uploads should share capacity with another background job.

    Skip cloud itself and treat failed probes as busy. Waiting or paused jobs
    consume no capacity; all probes are the same cheap snapshots as the strip.
    """
    for source in SOURCES:
        if source is _cloud:
            continue
        try:
            found = source(services, config)
            jobs = found if isinstance(found, list) else [found]
            if any(job and not job.get("paused") for job in jobs):
                return True
        except Exception:
            return True
    return False


def running(services: Any, config: Any = None) -> list[dict[str, Any]]:
    """Every background job going on right now, in the order to show them.

    *config* is the Flask app config, which is where anything built on first
    use lives — the video conversions, for one — rather than on `Services`.

    Cheap on purpose: every open tab polls this. No source may walk a folder,
    open a fresh database connection, or do work whose cost is paid per ask.

    Counting is allowed only behind a cache the source owns, and only when the
    answer is worth it: the cloud line counts the backup queue, which is one
    aggregate over 156,000 rows at about 7 ms, kept for eight seconds so that
    ten open tabs cost the same as one. Anything that cannot be capped that way
    does not belong here.
    """
    jobs: list[dict[str, Any]] = []
    for source in SOURCES:
        try:
            found = source(services, config)
        except Exception:                                 # noqa: BLE001
            # A subsystem that cannot say what it is doing must not take the
            # rest of the strip down with it: half an answer beats none, and
            # the poll behind this comes round again in two seconds.
            log.debug("could not ask %s what it is doing",
                      source.__name__, exc_info=True)
            continue
        if isinstance(found, list):
            jobs.extend(found)
        elif found:
            jobs.append(found)
    return jobs
