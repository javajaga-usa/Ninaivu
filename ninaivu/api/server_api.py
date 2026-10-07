"""The Server page's endpoints: the desktop control panel, in the console.

Everything the native panel shows that a running server can report about
itself - machine and process readings, the resource mode, the server log - and
the two things it can do to itself: restart (optionally into another resource
mode) and stop.

What it cannot do is start. A stopped Ninaivu has no console to press Start on,
so that stays with the desktop panel and start.cmd.

Console only, never the family port: a restart or a stop interrupts everyone.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from ..server import auth, capacity, runfile
from ..storage import db
from ..server.auth import current_user, require_admin
from ..utils.resources import MODES, budget
from ._body import json_body, json_object
from ..words import said

try:
    import psutil
except ImportError:                                      # pragma: no cover
    psutil = None

server_bp = Blueprint("server", __name__)

#: The checkout Ninaivu runs from; the desktop panel keeps its files beside it.
from ..desktop.control import control_dir, ninaivu_root, open_log
ROOT = ninaivu_root()
#: Where the desktop panel (and a restart from here) sends the server's output.
CONTROL_DIR = control_dir(ROOT)
SERVER_LOG = CONTROL_DIR / "server.log"
RESTART_LOG = CONTROL_DIR / "restart.log"

#: The most log text one request returns.
LOG_CHUNK = 64 * 1024
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

MODE_LABELS = {
    "standard": (said("Standard"), said("Balanced resources for daily use.")),
    "performance": (said("Performance"), said("More threads for intensive work.")),
    "power-saving": (said("Power-saving"), said("Fewer threads and gentler scheduling.")),
}


def _cfg():
    return current_app.config["MV_CONFIG"]


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

class _ProcessMeter:
    """CPU of this server and its children, as a share of the whole machine.

    psutil's per-process percentage needs a previous reading of that same
    process, so the times are kept between requests, as the desktop panel's
    Monitor keeps them between ticks.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.previous: dict[tuple[int, float], float] = {}
        self.last = time.monotonic()
        self.machine = None

    def machine_cpu(self):
        """The whole machine's CPU since the last reading.

        Not psutil.cpu_percent(): since psutil 7 its "since the last call"
        is kept per thread, and every request here arrives on a new thread, so
        it answered 0% every time.
        """
        times = psutil.cpu_times()
        busy = sum(times) - times.idle
        with self.lock:
            previous, self.machine = self.machine, (sum(times), busy)
        if previous is None:
            return psutil.cpu_percent(interval=0.1)
        total = sum(times) - previous[0]
        return max(0.0, min(100.0, (busy - previous[1]) * 100 / total)) if total > 0 else 0.0

    def sample(self):
        me = psutil.Process(os.getpid())
        try:
            processes = [me, *me.children(recursive=True)]
        except (psutil.Error, OSError):
            # A sandbox can refuse the list of children with a plain
            # PermissionError, which psutil.Error does not cover.
            processes = [me]
        with self.lock:
            now = time.monotonic()
            elapsed = max(0.01, now - self.last)
            self.last = now
            cpu = rss = threads = 0.0
            seen = {}
            for process in processes:
                try:
                    with process.oneshot():
                        ident = (process.pid, process.create_time())
                        times = process.cpu_times()
                        total = times.user + times.system
                        if ident in self.previous:
                            cpu += max(0.0, total - self.previous[ident]) / elapsed * 100
                        seen[ident] = total
                        rss += process.memory_info().rss
                        threads += process.num_threads()
                except (psutil.Error, OSError):
                    continue
            first = not self.previous
            self.previous = seen
        return {
            "cpu": None if first else min(100.0, cpu / (psutil.cpu_count() or 1)),
            "memory": int(rss),
            "threads": int(threads),
            "processes": len(seen),
            "started_at": capacity.reading(me.create_time),
        }


_meter = _ProcessMeter() if psutil is not None else None


class _PowerReading:
    """Battery discharge, read in the background.

    The reading is a PowerShell WMI query that can take seconds, so a request
    never waits on it: it gets the last value, and asks for a fresh one when
    that is older than REFRESH.
    """

    REFRESH = 30

    def __init__(self):
        self.lock = threading.Lock()
        self.value = None
        self.read_at = 0.0
        self.reading = False

    def get(self):
        with self.lock:
            stale = time.monotonic() - self.read_at > self.REFRESH
            if stale and not self.reading and sys.platform == "win32":
                self.reading = True
                threading.Thread(target=self._read, name="ninaivu-power",
                                 daemon=True).start()
            return self.value

    def _read(self):
        value = None
        try:
            from ..desktop.control import read_power              # noqa: PLC0415
            value = read_power()
        except Exception:                                         # noqa: BLE001
            value = None
        with self.lock:
            self.value, self.read_at, self.reading = value, time.monotonic(), False


_power = _PowerReading()


def _disk(path):
    if not path or psutil is None:
        return None
    try:
        usage = psutil.disk_usage(str(path))
    except (OSError, psutil.Error):
        return None
    return {"path": str(path), "drive": Path(path).anchor or str(path),
            "free": usage.free, "total": usage.total, "percent": usage.percent}


def _busy():
    """Work a restart or a stop would interrupt, in words for the dialog."""
    busy = []
    try:
        from ..archive import scanner as archive_scanner          # noqa: PLC0415
        if archive_scanner.is_scanning():
            busy.append("A consolidation is running. It stops at the file it is "
                        "on and has to be started again from the Archive page; "
                        "finished files are stepped over.")
    except Exception:                                             # noqa: BLE001
        pass
    scanner = current_app.config.get("MV_SCANNER")
    try:
        if scanner is not None and scanner.progress.snapshot().get("running"):
            busy.append("The library is being indexed. It picks up where it "
                        "stopped when Ninaivu starts again.")
    except Exception:                                             # noqa: BLE001
        pass
    return busy


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _network():
    """The Network access switch, and what this running server really does.

    The two can differ: the switch is saved at once but takes effect on the
    next start, and a server started with --local-only is local whatever the
    switch says.
    """
    from .admin_api import from_this_computer                   # noqa: PLC0415

    cfg = _cfg()
    family = (cfg.host or "") not in LOOPBACK_HOSTS
    console = (cfg.admin_host or cfg.host or "") not in LOOPBACK_HOSTS
    addresses = []
    if family:
        try:
            from ..utils import tls                                 # noqa: PLC0415
            addresses = tls.lan_addresses()
        except Exception:                                           # noqa: BLE001
            addresses = []
    return {
        "enabled": bool(getattr(cfg, "network_access", True)),
        "console_setting": bool(getattr(cfg, "console_on_network", False)),
        "family_on_network": family,
        "console_on_network": console,
        "addresses": addresses,
        # Turning it off from another device takes this page away from that
        # device; the page warns first.
        "request_is_local": from_this_computer(),
    }


def _endpoints(network):
    """Addresses that reach this server now, best first, for each app."""
    cfg = _cfg()
    hostnames = current_app.config.get("MV_HOSTNAMES") or {}
    scheme = current_app.config.get("MV_SCHEME") or "http"
    default = 443 if scheme == "https" else 80

    def url(host, port):
        return f"{scheme}://{host}" + ("" if int(port) == default else f":{int(port)}")

    family, console = [], []
    lan = network["addresses"][:1]
    if network["family_on_network"]:
        if hostnames.get("family"):
            family.append(url(hostnames["family"], cfg.port))
        family += [url(a, cfg.port) for a in lan]
    if network["console_on_network"]:
        if hostnames.get("admin"):
            # The named console is routed by name on the family port.
            console.append(url(hostnames["admin"], cfg.port))
        console += [url(a, cfg.admin_port) for a in lan]
    family.append(url("localhost", cfg.port))
    console.append(url("localhost", cfg.admin_port))
    # Away from home: whatever the household's remote-access provider says
    # works from outside (server/remote.py) — a Tailscale name, a tunnel's
    # public name, this computer's address on a WireGuard subnet. Listed so
    # the household can be given it.
    from ..server import remote                             # noqa: PLC0415
    access = remote.resolve(cfg)
    away_family, away_console = [], []
    for name in [*access.hostnames, *access.addresses]:
        if network["family_on_network"]:
            away_family.append(url(name, cfg.port))
        if network["console_on_network"]:
            away_console.append(url(name, cfg.admin_port))
    return {
        "scheme": scheme,
        "remote_access": access.describe(),
        "family": hostnames.get("family") if network["family_on_network"] else None,
        "admin": hostnames.get("admin") if network["console_on_network"] else None,
        "port": getattr(cfg, "port", None),
        "admin_port": getattr(cfg, "admin_port", None),
        "family_urls": family,
        "admin_urls": console,
        "tailnet_family_urls": away_family,
        "tailnet_admin_urls": away_console,
    }


@server_bp.get("/api/admin/server")
@require_admin
def server_state():
    cfg = _cfg()
    mode = budget()
    network = _network()
    payload = {
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "mode": mode["mode"],
        "budget": mode,
        # Set by the desktop panel; a server started some other way has no
        # chosen mode and runs on the standard budget.
        "mode_chosen": bool(os.environ.get("NINAIVU_RESOURCE_MODE")),
        "modes": [{"id": m, "label": MODE_LABELS[m][0], "description": MODE_LABELS[m][1],
                   **{k: v for k, v in budget(m).items() if k != "mode"}}
                  for m in MODES],
        "endpoints": _endpoints(network),
        "can_restart": _restart_problem() is None,
        "restart_problem": _restart_problem(),
        "can_stop": _stop_problem() is None,
        "stop_problem": _stop_problem(),
        # A restart under a service manager exits and is started again by it;
        # in a container the Network access switch cannot be turned off.
        "supervisor": _supervisor(),
        "in_container": runfile.in_container(),
        "restarting": _restart_in_progress(),
        "busy": _busy(),
        "network": network,
        "update": getattr(current_app.config.get("MV_SERVICES"), "updates", None).describe()
        if getattr(current_app.config.get("MV_SERVICES"), "updates", None) else None,
        "metrics": None,
    }
    if psutil is None:
        return jsonify(payload)

    # Each reading on its own: one the system will not give (a sandbox, a
    # container, an account without the permission) is left empty, and the
    # page still shows the rest.
    memory = capacity.reading(psutil.virtual_memory)
    battery = capacity.reading(psutil.sensors_battery)
    process = capacity.reading(_meter.sample)
    library = getattr(cfg, "active_root", None)
    disks = [d for d in (_disk(library), _disk(ROOT)) if d]
    if len(disks) == 2 and os.path.splitdrive(disks[0]["path"])[0].lower() == \
            os.path.splitdrive(disks[1]["path"])[0].lower() and os.name == "nt":
        disks = disks[:1]
    payload["metrics"] = {
        "at": time.time(),
        "cpu": capacity.reading(_meter.machine_cpu),
        "cpus": capacity.reading(psutil.cpu_count) or 1,
        "memory": {"percent": memory.percent, "used": memory.total - memory.available,
                   "total": memory.total} if memory is not None else
                  {"percent": None, "used": None, "total": None},
        "process": process,
        "battery": None if battery is None else {
            "percent": battery.percent, "plugged": battery.power_plugged},
        "power_watts": _power.get(),
        "disks": [dict(d, role="library" if library and d["path"] == str(library)
                       else "install") for d in disks],
    }
    return jsonify(payload)


# ---------------------------------------------------------------------------
# The server log
# ---------------------------------------------------------------------------

@server_bp.get("/api/admin/server/log")
@require_admin
def server_log():
    """Follow the server log a piece at a time.

    `after` is the byte offset the page already has. Nothing, a stale offset (the
    file was replaced), or one further behind than LOG_CHUNK returns the most
    recent LOG_CHUNK, starting at a whole line.
    """
    path = SERVER_LOG
    try:
        size = path.stat().st_size
        modified = path.stat().st_mtime
    except OSError:
        return jsonify({"available": False, "path": str(path), "text": "",
                        "position": 0, "reset": True})
    after = request.args.get("after", type=int)
    reset = after is None or after < 0 or after > size or size - after > LOG_CHUNK
    start = max(0, size - LOG_CHUNK) if reset else after
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read(LOG_CHUNK)
    position = start + len(data)
    # Hold back a trailing partial line (and any half of a UTF-8 character in
    # it) until it is finished, so a line is never shown in two pieces.
    cut = data.rfind(b"\n")
    if cut >= 0:
        position = start + cut + 1
        data = data[:cut + 1]
    else:
        position, data = start, b""
    text = data.decode("utf-8", errors="replace")
    if reset and start > 0:
        # The chunk began mid-line; drop that fragment.
        text = text.partition("\n")[2]
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    return jsonify({
        "available": True, "path": str(path), "text": text, "position": position,
        "reset": reset, "truncated": reset and start > 0, "size": size,
        "modified": modified,
    })


# ---------------------------------------------------------------------------
# Restart and stop
# ---------------------------------------------------------------------------

_restart_lock = threading.Lock()
_restart_process: subprocess.Popen | None = None


#: What each service manager is called on the page, for the messages below.
SUPERVISOR_LABELS = {
    "service": "the Windows scheduled task",
    "systemd": "systemd",
    "container": "the container's restart policy",
}


def _supervisor():
    """What starts this server again when it exits (see __main__.supervisor_of),
    or None when nothing does and a restart has to start the next one itself."""
    return current_app.config.get("MV_SUPERVISOR")


class _HandedToSupervisor:
    """Stands in for the helper process once a restart is left to the service
    manager: this server is on its way out and nothing else is to be started."""

    def poll(self):
        return None


def _restart_problem():
    if _supervisor():
        if not current_app.config.get("MV_RESTART"):
            return "This server cannot ask its service manager for a restart."
        return None
    if psutil is None:
        return "Restarting needs psutil, which is not installed in Ninaivu's environment."
    if not current_app.config.get("MV_STOP_TOKEN"):
        return "This server was not started with a stop token, so it cannot hand over to a new one."
    return None


#: How to stop Ninaivu for good under each supervisor. Stopping from the
#: console only ends this process, and the supervisor starts it again: the
#: Windows task's every-minute keep-alive within a minute, systemd and a
#: container's restart policy at once.
SUPERVISOR_STOP = {
    "service": "Ninaivu runs as a Windows scheduled task, which starts it again "
               "within a minute. Stop it with "
               "`installers\\windows\\install-service.ps1 -Action Stop`.",
    "systemd": "Ninaivu is run by systemd, which would start it again. Stop it "
               "through systemd (`systemctl stop ninaivu`).",
    "container": "Ninaivu runs in a container whose restart policy would start it "
                 "again. Stop the container instead (`docker stop ninaivu`).",
}


def _stop_problem():
    """Why the console's Stop cannot be used, or None when it can."""
    supervisor = _supervisor()
    if supervisor:
        return SUPERVISOR_STOP.get(
            supervisor, f"Ninaivu is started again by {supervisor}; stop it there.")
    if not current_app.config.get("MV_SHUTDOWN"):
        return "This server cannot be stopped from here."
    return None


def _restart_in_progress():
    return _restart_process is not None and _restart_process.poll() is None


def _spawn_relauncher(mode, network=None):
    """Start the process that stops this server and starts the next one.

    It must outlive this process, so it is started detached - on Windows in its
    own process group and, where allowed, outside any job object this server
    belongs to, which would otherwise take it down with the server.
    """
    command = [sys.executable, "-m", "ninaivu.desktop.relaunch"]
    if mode:
        command += ["--mode", mode]
    if network is not None:
        command += ["--network", "on" if network else "off"]
    # Owner-only, and trimmed when it has grown: it holds the addresses the
    # server answers on (ninaivu/desktop/control.py, open_log).
    log = open_log(RESTART_LOG)                        # handed to the child
    kwargs = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                  env=dict(os.environ, NINAIVU_STATE_DIR=str(_cfg().state_dir)))
    try:
        if os.name == "nt":
            detached = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                return subprocess.Popen(
                    command, creationflags=detached | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                    **kwargs)
            except OSError:
                # The job does not allow breakaway; a job that allows none also
                # does not kill on close, as a rule, so detached alone will do.
                return subprocess.Popen(command, creationflags=detached, **kwargs)
        return subprocess.Popen(command, start_new_session=True, **kwargs)
    finally:
        log.close()


def _begin_restart(mode=None, network=None, what=""):
    """Hand over to the helper; a (body, status) pair for the reply."""
    global _restart_process
    problem = _restart_problem()
    if problem:
        return {"error": problem}, 409
    supervisor = _supervisor()
    if supervisor and mode is not None and mode != budget()["mode"]:
        # The worker budget comes from how the service starts the server, which
        # a restart from here cannot rewrite; restarting would only look as if
        # the mode had changed.
        return {"error": "Ninaivu is started by "
                         f"{SUPERVISOR_LABELS.get(supervisor, supervisor)}, so its "
                         "resource mode is set where that service is configured "
                         "(NINAIVU_RESOURCE_MODE or --workers)."}, 409
    with _restart_lock:
        if _restart_in_progress():
            return {"error": "A restart is already under way."}, 409
        if supervisor:
            # Exit, and let the service manager start the next one. A helper
            # that started it would leave it outside the manager: stopping the
            # service would no longer stop Ninaivu, and in a container the
            # helper dies with the server it replaced. A network choice needs
            # nothing more; it is saved and honoured at start-up.
            _restart_process = _HandedToSupervisor()
            threading.Timer(0.5, current_app.config["MV_RESTART"]).start()
        else:
            try:
                _restart_process = _spawn_relauncher(mode, network)
            except OSError as exc:
                return {"error": f"Could not start the restart: {exc}"}, 500
    user = current_user()
    auth.audit(db.connect(_cfg().db_path), getattr(user, "id", None), "restart",
               f"restart from the console{what}")
    return {"restarting": True, "mode": mode or budget()["mode"],
            "pid": os.getpid()}, 202


@server_bp.post("/api/admin/server/restart")
@require_admin
def restart_server():
    data = json_object()
    mode = data.get("mode") or None
    if mode is not None and mode not in MODES:
        return jsonify({"error": "Unknown resource mode."}), 400
    body, status = _begin_restart(mode, what=f" into {mode} mode" if mode else "")
    return jsonify(body), status


@server_bp.post("/api/admin/server/network")
@require_admin
def set_network_access():
    """The Network access switch: save it, then restart so it takes effect.

    Saved first, so it holds even if the restart cannot happen from here - the
    next start by any launcher honours it.
    """
    data = json_object()
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "Say whether network access should be on or off."}), 400
    if not enabled and runfile.in_container():
        # Off means listening on 127.0.0.1, which inside a container is the
        # container's own: the published ports would reach nothing, this page
        # included, and nothing here could turn it back on.
        return jsonify({"error": "Ninaivu is running in a container, where switching "
                                 "network access off would leave nothing able to "
                                 "reach it, this page included. Keep it to this "
                                 "computer by publishing the ports on 127.0.0.1 "
                                 "instead (for example 127.0.0.1:5000:5000)."}), 409
    cfg = _cfg()
    cfg.network_access = enabled
    cfg.save()
    body, status = _begin_restart(
        network=enabled,
        what=" to turn network access " + ("on" if enabled else "off"))
    if status != 202:
        # Saved, but not applied; the page says so rather than claiming it is.
        body = {"saved": True, "applied": False, **body}
        return jsonify(body), 200 if status == 409 else status
    body.update(saved=True, applied=True, network_access=enabled)
    return jsonify(body), 202


@server_bp.post("/api/admin/server/console-network")
@require_admin
def set_console_network():
    """Open the console to other devices at home, or keep it to this computer.

    Saved, then a restart, as the Network access switch does. Refused from
    another device when closing it would take the page away from the person
    asking without them knowing — the page says so first.
    """
    data = json_object()
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "Say whether the console should be open to the network."}), 400
    cfg = _cfg()
    cfg.console_on_network = enabled
    cfg.save()
    body, status = _begin_restart(what=" to " + ("open the console to the network" if enabled
                                                  else "keep the console to this computer"))
    if status != 202:
        body = {"saved": True, "applied": False, **body}
        return jsonify(body), 200 if status == 409 else status
    body.update(saved=True, applied=True, console_on_network=enabled)
    return jsonify(body), 202


@server_bp.post("/api/admin/server/stop")
@require_admin
def stop_server():
    problem = _stop_problem()
    if problem:
        return jsonify({"error": problem}), 409
    stopper = current_app.config["MV_SHUTDOWN"]
    user = current_user()
    auth.audit(db.connect(_cfg().db_path), getattr(user, "id", None), "shutdown",
               "stopped from the console")
    # Answer first; the stop runs on its own thread and lets this reply go out.
    threading.Timer(0.5, stopper).start()
    return jsonify({"stopping": True}), 202


# ---------------------------------------------------------------------------
# Sharing the machine with the household
# ---------------------------------------------------------------------------

@server_bp.get("/api/admin/workload")
@require_admin
def workload_status():
    """How background work shares the machine, and what it is doing now."""
    return jsonify(current_app.config["MV_SERVICES"].workload.snapshot())


@server_bp.post("/api/admin/workload")
@require_admin
def workload_settings():
    """Choose Balanced, Quiet or Overnight, and the night hours."""
    from ..server import workload as workload_mod            # noqa: PLC0415

    data = json_body()
    if not isinstance(data, dict) or set(data) - {"mode", "night_start", "night_end"}:
        return jsonify({"error": "Send mode, night_start and night_end.",
                        "status": 400}), 400
    cfg = current_app.config["MV_CONFIG"]
    mode = data.get("mode", cfg.workload_mode)
    if mode not in workload_mod.MODES:
        return jsonify({"error": "Choose balanced, quiet or overnight.",
                        "status": 400}), 400
    start = workload_mod.valid_clock(data.get("night_start", cfg.workload_night_start))
    end = workload_mod.valid_clock(data.get("night_end", cfg.workload_night_end))
    if start is None or end is None or start == end:
        return jsonify({"error": "Use two different 24-hour times for the night, "
                        "like 23:00 and 06:00.", "status": 400}), 400
    cfg.workload_mode, cfg.workload_night_start, cfg.workload_night_end = mode, start, end
    cfg.save()
    # Takes effect at once: every job asks again before its next piece of work.
    current_app.config["MV_SERVICES"].cloud.apply_settings()
    auth.audit(db.connect(cfg.db_path), current_user().id, "workload",
               f"{mode}, night {start}–{end}")
    return jsonify(current_app.config["MV_SERVICES"].workload.snapshot())


# ---------------------------------------------------------------------------
# Performance (ninaivu/server/capacity.py)
# ---------------------------------------------------------------------------

@server_bp.get("/api/admin/performance")
@require_admin
def performance_report():
    """What this computer can do for Ninaivu, and what would help it do more."""
    from .. import ai                                     # noqa: PLC0415
    return jsonify(capacity.assess(_cfg(), db.connect(_cfg().db_path), ai.get_engine()))


# ---------------------------------------------------------------------------
# Tuning (ninaivu/server/tuning.py)
# ---------------------------------------------------------------------------

def _tuning_report(result=None):
    from .. import ai                                     # noqa: PLC0415
    from ..server import tuning                           # noqa: PLC0415
    cfg = _cfg()
    result = result or tuning.current(cfg, ai.get_engine())
    result["restart_needed"] = tuning.restart_needed(cfg, result["values"])
    result["can_restart"] = _restart_problem() is None
    return result


@server_bp.get("/api/admin/tuning")
@require_admin
def tuning_report():
    """The machine, the profile chosen for it, every knob and what it is expected to use."""
    return jsonify(_tuning_report())


@server_bp.post("/api/admin/tuning")
@require_admin
def tuning_settings():
    """Choose a profile, set knobs outright or put them back to automatic.

    ``{"profile": "peak"}``, ``{"values": {"workers": 6, "db_cache_mb": null}}``
    (null is back to automatic), or ``{"reset": true}`` for everything.
    """
    from .. import ai                                     # noqa: PLC0415
    from ..server import tuning                           # noqa: PLC0415

    data = json_object()
    if set(data) - {"profile", "values", "reset"}:
        return jsonify({"error": "Send profile, values or reset."}), 400
    cfg = _cfg()
    try:
        tuning.save(cfg, data.get("profile"), data.get("values"), bool(data.get("reset")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    cfg.save()
    result = tuning.apply(cfg, ai.get_engine(), current_app.config.get("MV_SERVICES"))
    auth.audit(db.connect(cfg.db_path), current_user().id, "tuning",
               "back to automatic" if data.get("reset") else
               f"{result['profile']}: " + ", ".join(f"{k} {v}" for k, v in result["values"].items()))
    return jsonify(_tuning_report(result))


# ---------------------------------------------------------------------------
# Drive health (ninaivu/storage/disk_health.py)
# ---------------------------------------------------------------------------

@server_bp.get("/api/admin/disks")
@require_admin
def disks_report():
    """Every drive Windows can see, and what it has logged about each."""
    return jsonify(current_app.config["MV_SERVICES"].disks.report())


@server_bp.post("/api/admin/disks/check")
@require_admin
def disks_check():
    """Look now rather than at the next ten-minute check."""
    return jsonify(current_app.config["MV_SERVICES"].disks.check())


# ---------------------------------------------------------------------------
# Is everything safe? (ninaivu/server/safety.py)
# ---------------------------------------------------------------------------

@server_bp.get("/api/admin/safety")
@require_admin
def safety_report():
    """Every protection Ninaivu has, asked at once. ``?fresh=1`` asks again now."""
    fresh = request.args.get("fresh") in ("1", "true")
    return jsonify(current_app.config["MV_SERVICES"].safety.report(fresh=fresh))
