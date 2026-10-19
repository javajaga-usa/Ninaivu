"""The model host: one process that owns the on-device engine's models.

Models used to be loaded inside the server: PyTorch, its gigabytes and its
threads, in the same process that answers the family's pages. A model that
ran out of memory, or a native library that crashed on one odd file, took the
gallery down with it, and the memory a model held stayed held for as long as
the server ran. The host is a process of its own instead:

* **It owns every model.** The rest of Ninaivu asks it for a *capability* by
  name ("transcribe", and in later steps "caption", "embed") with plain values,
  and gets plain values back. Nothing of the server's crosses over.
* **A crash costs one answer.** When the host dies mid-call the caller gets
  :class:`HostCrashed` and decides (a story is tried again, then set aside);
  the next call starts a fresh host.
* **Idle, it goes.** After :data:`IDLE_SECONDS` with nothing asked it exits
  and its memory is the computer's again; the next call starts it. Whisper
  large-v3 is about 2.5 GB in memory, which a 16 GB Mac running Immich beside
  Ninaivu should not hold through the day for one story a week.
* **It is polite.** It lowers its own priority, so a page the family opens
  is answered first while it works.
* **One call at a time.** A capability call can take minutes (a long story);
  a second caller waits its turn. Enough for this first step, where the host
  has one capability; the job engine (1.13, ``ninaivu/engine``) is where
  scheduling between many belongs, and it can run its tasks against this
  host as it runs them against its worker processes.

Capabilities are found by a ``"module:attribute"`` path naming a
:class:`Capability`, so the host imports only what it is asked for.
"""
from __future__ import annotations

import importlib
import logging
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Seconds without a call after which the host exits and frees its memory.
IDLE_SECONDS = 600.0

#: Every capability the host can be asked for, by name, and where it is.
CAPABILITIES: dict[str, str] = {
    "transcribe": "ninaivu.runtime.speech:TRANSCRIBE",
    "search": "ninaivu.runtime.siglip:SEARCH",
    "describe": "ninaivu.runtime.florence:DESCRIBE",
    "generate": "ninaivu.runtime.llm:GENERATE",
}


class HostError(RuntimeError):
    """A call to the model host did not give an answer."""


class HostCrashed(HostError):
    """The host process ended while it was working on the call."""


class HostTimeout(HostError):
    """The call took longer than it was allowed; the host was stopped."""


class HostStopped(HostError):
    """The call was abandoned because Ninaivu is stopping."""


class HostBusy(HostError):
    """The host was working on another call for longer than this caller
    would wait: a page asking for a draft is not held for the minutes a long
    story takes to be written down (audit C-12)."""


class Unavailable(HostError):
    """The capability cannot run on this computer as it is: its model is not
    downloaded, or the package it needs is not installed, or its model could
    not be loaded. Not a fault of the file it was asked about, so nothing is
    marked as failed for it."""


#: After a model fails to load, how long before the host is started for it
#: again, at first and at most: the wait doubles each time it fails again.
LOAD_BACKOFF = (30.0, 600.0)


@dataclass(frozen=True)
class Capability:
    """One thing the host can be asked to do.

    ``load(setup)`` runs once in the host, the first time the capability is
    asked for, and returns whatever it keeps (its models); it raises
    :class:`Unavailable` when it cannot run here. ``run(kept, **kwargs)``
    answers one call. ``version`` is stamped on what it makes, so a better
    model makes its own results owed again and nothing else.
    """

    name: str
    version: int
    load: Callable[[dict[str, Any]], Any]
    run: Callable[..., Any]


def resolve(path: str) -> Capability:
    module_name, _, attribute = path.partition(":")
    capability = getattr(importlib.import_module(module_name), attribute)
    if not isinstance(capability, Capability):
        raise TypeError(f"{path} is not a capability")
    return capability


# ---------------------------------------------------------------------------
# Inside the host
# ---------------------------------------------------------------------------

def _settle_in(setup: dict[str, Any]) -> None:
    """Set up the host process: where the models are, and a lower priority."""
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s model-host %(levelname)s %(name)s: %(message)s")
    from ..media import model_catalog                          # noqa: PLC0415
    model_catalog.configure(setup.get("models") or None)
    try:
        if hasattr(os, "nice"):
            os.nice(5)
        else:                                                   # Windows
            import psutil                                       # noqa: PLC0415
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:                                           # noqa: BLE001 - only a courtesy
        pass


def serve(conn: Any, setup: dict[str, Any]) -> None:
    """The host's loop: answer calls until told to stop, or idle too long."""
    _settle_in(setup)
    known = {**CAPABILITIES, **setup.get("capabilities", {})}
    kept: dict[str, tuple[Capability, Any]] = {}
    #: Capabilities whose load failed in this host, and why. A model file
    #: with the right size and the wrong bytes, or a library too old for it,
    #: fails the same way for every photograph: it is said once, as
    #: unavailable, and not loaded again for every call (audit C-11).
    broken: dict[str, str] = {}
    idle = float(setup.get("idle_seconds", IDLE_SECONDS))
    while True:
        try:
            if not conn.poll(idle):
                break                                           # idle: give the memory back
            message = conn.recv()
        except (EOFError, OSError):
            break                                               # the server has gone
        if message is None:
            break
        call_id, name, kwargs = message
        try:
            if name in broken:
                raise Unavailable(broken[name])
            if name not in kept:
                if name not in known:
                    raise Unavailable(f"the model host has no capability called {name}")
                try:
                    capability = resolve(known[name])
                    kept[name] = (capability, capability.load(setup))
                except Unavailable:
                    raise
                except Exception as exc:                        # noqa: BLE001 - the model's, not the file's
                    log.warning("%s could not be loaded", name, exc_info=True)
                    broken[name] = f"the model could not be loaded: {type(exc).__name__}: {exc}"
                    raise Unavailable(broken[name]) from None
                # Loaded: from here a crash is the call's, not the model's.
                conn.send((call_id, "loaded", name))
            capability, state = kept[name]
            reply = (call_id, "ok", capability.run(state, **kwargs))
        except Unavailable as exc:
            reply = (call_id, "unavailable", str(exc))
        except Exception as exc:                                # noqa: BLE001 - sent back
            log.warning("%s failed", name, exc_info=True)
            reply = (call_id, "error", f"{type(exc).__name__}: {exc}")
        try:
            conn.send(reply)
        except (EOFError, OSError):
            break


# ---------------------------------------------------------------------------
# The server's side
# ---------------------------------------------------------------------------

class ModelHost:
    """The server's handle on the host process, started on the first call."""

    def __init__(self, setup: dict[str, Any] | None = None, *,
                 context: Any = None) -> None:
        self._setup = dict(setup or {})
        self._idle = float(self._setup.setdefault("idle_seconds", IDLE_SECONDS))
        # Spawned, never forked: a fork would copy the server's threads, locks
        # and open databases into a process that does not know it has them.
        self._ctx = context or multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._process: Any = None
        self._conn: Any = None
        self._last = 0.0
        self._next_id = 0
        self._closed = False
        self._state: dict[str, Any] = {"calls": 0, "crashes": 0, "starts": 0,
                                       "busy": "", "last_error": ""}
        #: What this host process has loaded, as it has said so.
        self._loaded: set[str] = set()
        #: No host started for these before the time given (time.monotonic()),
        #: after their model failed to load: with the wait so far.
        self._not_before: dict[str, tuple[float, float]] = {}

    # -- the process ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _start(self) -> None:
        self._drop()
        parent, child = self._ctx.Pipe()
        process = self._ctx.Process(target=serve, args=(child, self._setup),
                                    name="ninaivu-model-host", daemon=True)
        process.start()
        child.close()
        self._process, self._conn = process, parent
        self._loaded = set()
        self._state["starts"] += 1

    def _ready(self) -> None:
        """A host that will still be there for the call about to be sent.

        One about to leave on its own (idle for nearly its limit) is replaced
        first: a call sent as it exits would read as a crash.
        """
        if self.running and time.monotonic() - self._last < self._idle - 5:
            return
        self._start()

    def _drop(self, *, kill: bool = False) -> None:
        process, conn = self._process, self._conn
        self._process = self._conn = None
        if conn is not None:
            try:
                if not kill:
                    conn.send(None)
            except (OSError, EOFError):
                pass
            conn.close()
        if process is not None:
            process.join(0 if kill else 5)
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)

    def recycle(self) -> None:
        """End the host after the call in progress, if any; the next call
        starts a fresh one, which loads the models downloaded since."""
        with self._lock:
            self._not_before.clear()
            self._drop()

    def _could_not_load(self, capability: str, why: str) -> Unavailable:
        """Note that *capability*'s model would not load, and hold off
        starting a host for it again for a while."""
        first, most = LOAD_BACKOFF
        _, waited = self._not_before.get(capability, (0.0, 0.0))
        wait = min(most, waited * 2) if waited else first
        self._not_before[capability] = (time.monotonic() + wait, wait)
        self._state["last_error"] = why
        return Unavailable(why)

    def close(self) -> None:
        """Stop the host for good; a call in progress ends with HostStopped."""
        self._closed = True
        with self._lock:
            self._drop()

    # -- calls ----------------------------------------------------------------

    def call(self, capability: str, *, timeout: float | None = None,
             stop: threading.Event | None = None, wait: float | None = None,
             **kwargs: Any) -> Any:
        """Ask the host for *capability* and wait for its answer.

        Raises :class:`Unavailable` when it cannot run here, :class:`HostCrashed`
        when the host died working on it, :class:`HostTimeout` after *timeout*
        seconds and :class:`HostStopped` once *stop* is set or the host is
        closed; any other failure inside the capability is a :class:`HostError`
        with its message. With *wait*, :class:`HostBusy` when another call
        held the host for longer than that many seconds before this one began.
        """
        if not self._lock.acquire(timeout=-1 if wait is None else max(0.0, float(wait))):
            raise HostBusy(f"the model host is busy with {self._state['busy'] or 'another call'}")
        try:
            if self._closed:
                raise HostStopped("the model host is closed")
            held = self._not_before.get(capability)
            if held is not None and time.monotonic() < held[0]:
                raise Unavailable(self._state["last_error"] or
                                  f"{capability}: its model could not be loaded")
            self._state["busy"] = capability
            try:
                answer = self._call(capability, timeout, stop, kwargs, retry=True)
            except Unavailable as exc:
                if str(exc).startswith("the model could not be loaded"):
                    raise self._could_not_load(capability, str(exc)) from None
                raise
            self._not_before.pop(capability, None)
            return answer
        finally:
            self._state["busy"] = ""
            self._last = time.monotonic()
            self._lock.release()

    def _call(self, capability: str, timeout: float | None, stop: threading.Event | None,
              kwargs: dict[str, Any], *, retry: bool) -> Any:
        self._ready()
        self._next_id += 1
        call_id = self._next_id
        self._state["calls"] += 1
        try:
            self._conn.send((call_id, capability, kwargs))
        except (OSError, EOFError):
            if not retry:
                raise HostCrashed("the model host could not be reached") from None
            self._start()
            return self._call(capability, timeout, stop, kwargs, retry=False)
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            if self._closed or (stop is not None and stop.is_set()):
                self._drop(kill=True)
                raise HostStopped("stopped while the model host was working")
            if deadline is not None and time.monotonic() > deadline:
                self._drop(kill=True)
                self._state["last_error"] = f"{capability} took longer than {timeout:g} s"
                raise HostTimeout(self._state["last_error"])
            try:
                if self._conn.poll(0.25):
                    reply = self._conn.recv()
                    if reply[1] == "loaded":
                        self._loaded.add(capability)
                        continue
                    break
            except (OSError, EOFError):
                reply = None
                break
            if not self._process.is_alive():
                reply = None
                break
        if reply is None:
            code = self._process.exitcode if self._process is not None else None
            self._drop(kill=True)
            if code == 0 and retry:
                # It left on its own just as the call arrived: not a crash.
                return self._call(capability, timeout, stop, kwargs, retry=False)
            self._state["crashes"] += 1
            if capability not in self._loaded:
                # It died loading the model (a native library that crashes on
                # a damaged file): the model's fault, not the photograph's,
                # and starting a fresh host for every batch only crashes again.
                raise self._could_not_load(
                    capability, f"the model could not be loaded: the model host stopped "
                                f"(exit code {code}) while loading {capability}")
            self._state["last_error"] = f"the model host stopped (exit code {code}) during {capability}"
            raise HostCrashed(self._state["last_error"])
        answered, outcome, value = reply
        if answered != call_id:                     # an answer to an abandoned call
            raise HostCrashed("the model host answered another call")
        if outcome == "ok":
            return value
        self._state["last_error"] = str(value)
        if outcome == "unavailable":
            raise Unavailable(str(value))
        raise HostError(str(value))

    def status(self) -> dict[str, Any]:
        process = self._process
        return {**self._state, "running": self.running,
                "pid": process.pid if process is not None and self.running else None}
