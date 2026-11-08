"""The family app on the Secondary while the Primary is off, from the Secondary's own Ninaivu.

With **Let the family browse on the Secondary while this computer is off**
ticked on the Primary, the Primary sends the Secondary a hand-over
(mugil/handover.py) into ``mugil-handover/``. Until 4.0 a second, read-only
Ninaivu served it as a service of its own: a hand-written systemd unit, the
originals bind mounted at the Primary's paths with ``--map``, and sudo to set
it up. That way still works (``--replica-of``, ``replica unit``), and is the
old way.

Now the Secondary's own Ninaivu serves it, on its own family app (port 80),
when this computer is a paired Secondary and a hand-over is there:

* **While the Primary does not answer**, the family app is the hand-over,
  read-only: the newest snapshot that checks out (replica/receive.py), and
  every change refused but signing in and out (replica/front.py).
* **While the Primary is awake**, a page opened here is sent to the same page
  on the Primary, as front.py always did.
* **The paths are this computer's.** Each library in the snapshot is
  re-rooted onto the folder the Secondary keeps its originals in, through its
  own record of which library is in which folder (mugil/hold.py, library id
  to folder). The copy of the index served is the Secondary's own (in
  ``mugil-family-view/``), so nothing the Primary sent is changed, and
  nothing on the originals disk is ever written.

What stays the Secondary's own on its family port: the routes between the
two computers (``/api/mugil/``), the phone inbox (``/dav``) and the health
check. Everything else goes to the family view while it is served, and to
the Secondary's own app when it is not.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import SECONDARY, SINGLE, pairing, role

log = logging.getLogger(__name__)

__all__ = ["FamilyView", "wanted", "reroot_index", "install", "STATE", "OWN_PATHS",
           "LOOK_EVERY"]

#: In the Secondary's state folder: the family view's own state (the copy of
#: the index served, its sign-ins, the links to the thumbnails).
STATE = "mugil-family-view"
#: In :data:`STATE`: the Primary's library folders, by their ids, as the last
#: hand-over named them, for re-rooting a copy taken up before a restart.
LIBRARIES = "family-view.json"

#: Paths the Secondary answers itself, never the family view.
OWN_PATHS = ("/api/mugil/", "/dav/")
OWN_EXACT = frozenset({"/dav", "/healthz", "/readyz"})

#: Seconds between two looks for a newer hand-over.
LOOK_EVERY = 60.0
#: Seconds a look at whether the family view is wanted is taken as said.
WANTED_FOR = 5.0


def wanted(cfg) -> bool:
    """Whether this computer serves the family view: a paired Secondary with
    a hand-over from the Primary in ``mugil-handover/``."""
    from ..replica import MANIFEST                                 # noqa: PLC0415
    from .handover import folder                                   # noqa: PLC0415
    if role(cfg) != SECONDARY or pairing.load(cfg.state_dir) is None:
        return False
    return (folder(cfg.state_dir) / MANIFEST).is_file()


def reroot_index(index: Path | str, mapping: dict[str, str]) -> int:
    """Rewrite each library folder named in *mapping* (the Primary's path to
    this computer's) in the copy of the index at *index*. Returns the rows
    changed. Every table that records a row's library folder is rewritten
    (storage/reroot.py ROOT_TABLES), so nothing points at a path that is not
    here."""
    from ..storage.reroot import ROOT_TABLES                       # noqa: PLC0415
    changed = 0
    moves = {old: new for old, new in mapping.items() if old and new and old != new}
    if not moves:
        return 0
    with closing(sqlite3.connect(str(index), timeout=30.0)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ROOT_TABLES:
            if table not in tables:
                continue
            if "root" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                continue
            for old, new in moves.items():
                changed += conn.execute(f"UPDATE OR IGNORE {table} SET root=? WHERE root=?",
                                        (new, old)).rowcount
        conn.commit()
    return changed


def _build_app(view_cfg, replica, front):
    """The family app over the view's own copy of the index, as the old
    ``--replica-of`` process built it (ninaivu/__main__.py), never started."""
    from .. import __version__, build_services, create_home_app   # noqa: PLC0415
    services = build_services(view_cfg)
    services.replica = replica
    services.replica_front = front
    app = create_home_app(services)
    app.config["APP_VERSION"] = __version__
    return app


class FamilyView:
    """The Secondary's family view: built when it is wanted, kept up to date."""

    def __init__(self, cfg, *, build: Callable[..., Any] | None = None,
                 opener: Callable[..., Any] | None = None) -> None:
        self.cfg = cfg
        self._build = build or _build_app
        self._opener = opener
        self.app = None
        self.replica = None
        self.front = None
        self.view_cfg = None
        self.error = ""
        self._wanted = (0.0, False)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> Path:
        return Path(self.cfg.state_dir) / STATE

    def source(self) -> Path:
        from .handover import folder                               # noqa: PLC0415
        return folder(self.cfg.state_dir)

    def wanted(self) -> bool:
        at, said = self._wanted
        now = time.monotonic()
        if now - at > WANTED_FOR:
            said = wanted(self.cfg)
            self._wanted = (now, said)
        return said

    # -- where the Primary is ---------------------------------------------

    def _primary_url(self, manifest: dict[str, Any]) -> str:
        """The Primary's family app, as this computer reaches it: the address
        the pairing last saw it at, and the port the hand-over named."""
        from urllib.parse import urlsplit                          # noqa: PLC0415
        pair = pairing.load(self.cfg.state_dir) or {}
        url = str(pair.get("partner_url") or "")
        host = str(pair.get("partner_address") or urlsplit(url).hostname or "")
        if not host:
            return ""
        scheme = "https" if url.startswith("https://") else "http"
        port = int(manifest.get("family_port") or 80)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        return f"{scheme}://{host}" + ("" if default else f":{port}")

    def _wake_mac(self) -> str:
        from . import wake                                         # noqa: PLC0415
        macs = wake.load(self.cfg.state_dir).get("macs") or []
        return str(macs[0]) if macs else ""

    # -- the view's own configuration --------------------------------------

    def _manifest(self) -> dict[str, Any]:
        from ..replica import MANIFEST                             # noqa: PLC0415
        try:
            data = json.loads((self.source() / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _make_view_cfg(self):
        view = copy.copy(self.cfg)
        view.state_dir = self.state
        view.index_file = ""
        view.mugil_role = SINGLE
        view.watch = False
        view.roots = []
        view.active_root = ""
        view.replica_from = str(self.source())
        manifest = self._manifest()
        view.replica_primary_url = self._primary_url(manifest)
        view.replica_wake_mac = self._wake_mac()
        return view

    def _libraries(self, manifest: dict[str, Any] | None) -> dict[str, str]:
        """The Primary's library folders by id: from the hand-over being
        taken up, remembered for a restart; else as remembered."""
        path = self.state / LIBRARIES
        found = (manifest or {}).get("libraries")
        if isinstance(found, dict):
            from ..utils.files import atomic_write                 # noqa: PLC0415
            atomic_write(path, json.dumps({"libraries": found}, indent=2))
            return {str(k): str(v) for k, v in found.items()}
        try:
            known = json.loads(path.read_text(encoding="utf-8")).get("libraries") or {}
        except (OSError, ValueError, AttributeError):
            known = {}
        return {str(k): str(v) for k, v in known.items()} if isinstance(known, dict) else {}

    def _reroot(self, index: Path, manifest: dict[str, Any] | None) -> None:
        """Put each of the Primary's libraries in *index* where this computer
        keeps its originals, and give the view those folders."""
        from .hold import Holding                                  # noqa: PLC0415
        folder = str(getattr(self.cfg, "mugil_originals_dir", "") or "")
        here = Holding(folder).library_folders() if folder else {}
        mapping = {}
        for root, library in self._libraries(manifest).items():
            if library in here:
                mapping[root] = str(here[library])
            else:
                log.info("the family view has no folder here for %s yet: its "
                         "photographs open once the Secondary keeps them", root)
        changed = reroot_index(index, mapping)
        if changed:
            log.info("family view: %s rows re-rooted onto the originals here", f"{changed:,}")
        roots = list(mapping.values())
        if self.view_cfg is not None:
            self.view_cfg.roots = roots
            self.view_cfg.active_root = roots[0] if roots else ""

    # -- building and keeping it -------------------------------------------

    def look(self) -> bool:
        """Build the view if it is wanted and not built, or take up a newer
        hand-over. Returns whether the view is served. Never raises."""
        from ..replica.receive import ReplicaError                 # noqa: PLC0415
        if not self.wanted():
            return False
        try:
            with self._lock:
                if self.app is None:
                    self._build_now()
                else:
                    self.replica.look()
                    self.error = self.replica.error
        except ReplicaError as exc:
            if str(exc) != self.error:
                log.warning("family view: %s", exc)
            self.error = str(exc)
        except Exception as exc:                                    # noqa: BLE001
            log.exception("the family view could not be served")
            self.error = f"The family view could not be served: {exc}"
        return self.app is not None

    def _build_now(self) -> None:
        from ..replica.front import Front                          # noqa: PLC0415
        from ..replica.receive import Replica                      # noqa: PLC0415
        self.state.mkdir(parents=True, exist_ok=True)
        self.view_cfg = self._make_view_cfg()
        replica = Replica(self.view_cfg, self.source(), shared=True, on_point=self._reroot)
        if not replica.resume():
            replica.take_newest()                                  # raises ReplicaError
        else:
            replica.look()
        front = Front(self.view_cfg, replica, **({"opener": self._opener}
                                                 if self._opener else {}))
        self.app = self._build(self.view_cfg, replica, front)
        self.replica, self.front = replica, front
        self.error = replica.error
        front.keep_looking()
        log.info("the family view is served here while the Primary is off")

    def keep(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mugil-family-view",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self.look()
        while not self._stop.wait(LOOK_EVERY):
            self.look()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if self.front is not None:
            self.front.stop()
        if join and self._thread is not None:
            self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"wanted": self.wanted(), "served": self.app is not None,
                               "error": self.error}
        if self.front is not None:
            out.update(self.front.status())
        return out

    # -- answering ---------------------------------------------------------

    def app_for(self, path: str):
        """The app to answer *path* on the family port: the view while it is
        served, or None for the Secondary's own."""
        if path in OWN_EXACT or path.startswith(OWN_PATHS):
            return None
        if self.app is None or not self.wanted():
            return None
        return self.app


class _Dispatch:
    """The Secondary's family port: its own app for its own paths, the family
    view for everything else while it is served."""

    def __init__(self, own, view: FamilyView) -> None:
        self.own = own
        self.view = view

    def __call__(self, environ, start_response):
        target = self.view.app_for(environ.get("PATH_INFO", "") or "/")
        if target is None:
            return self.own(environ, start_response)
        return target(environ, start_response)


def install(app, view: FamilyView) -> None:
    """Put the family view in front of *app*, the Secondary's family app."""
    app.config["MV_FAMILY_VIEW"] = view
    app.wsgi_app = _Dispatch(app.wsgi_app, view)
