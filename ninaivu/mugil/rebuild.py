"""On a new Primary: the library rebuilt from the originals the Secondary keeps.

The Mac that was the Primary is gone, or its library disk failed, and the
Secondary still keeps every original that was sent to it. Pairing a new Mac
and pressing Rebuild copies them back, library by library, into a folder on
the new Mac. It only ever copies: nothing is removed or replaced on either
computer, so a rebuild can be stopped and started again, and a file already
in the folder is left as it is.

Each original arrives as it is kept, and is kept here only when it is that
very file:

* its size and fingerprint match what the Secondary recorded when it kept
  it;
* it gets its own modified time back, and nothing else about it changes
  (the bytes are the bytes that were sent, EXIF and all);
* it is written under a temporary name and renamed, so a rebuild cut short
  never leaves half a photograph at a library path.

The folder is given the library's own id (``.ninaivu-library``), so the
Secondary knows it as the same library and the next run sends nothing again.
A folder that is not a library yet is added as one and indexed.

To keep names, albums and stories too, bring the old Mac's state folder over
first (``--bring``, or a copy of the index) and rebuild into the folder the
library was at: the index then finds every photograph where it was.

While the Secondary is away the rebuild waits for it and carries on.
Nothing here runs with one computer.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import PRIMARY, pairing, role
from .courier import AWAY, AWAY_FIRST, AWAY_LONGEST, TIMEOUT, Partner, PartnerError
from .hold import clean_path, hasher
from ..storage import library_id

log = logging.getLogger(__name__)

#: How many kept files are asked for at a time.
PAGE = 500
#: What the console says while the Secondary is away (as a "reason", so
#: tools/server_messages.py collects it for translation).
AWAY_SAID = {"reason": "The Secondary is not answering. The rebuild carries on when it is back."}
AWAY_WORDS = AWAY_SAID["reason"]


def libraries(cfg, partner: Partner | None = None) -> list[dict[str, Any]]:
    """The libraries the Secondary keeps, each with the folder it was at on
    this computer when that is known."""
    from .courier import SENT, sent_record                        # noqa: PLC0415
    pair = pairing.load(cfg.state_dir) or {}
    partner = partner or Partner(cfg.mugil_partner_url, pair.get("key"), timeout=TIMEOUT)
    hello = partner.request("GET", "/api/mugil/hello")
    was: dict[str, str] = {}
    if (Path(cfg.state_dir) / SENT).exists():
        with sent_record(cfg.state_dir) as conn:
            was = {r["library"]: r["root"] for r in conn.execute("SELECT root, library FROM libraries")}
    out = []
    for lib in (hello.get("keeping") or {}).get("libraries") or []:
        if not isinstance(lib, dict) or not lib.get("id"):
            continue
        out.append({"id": str(lib["id"]), "name": str(lib.get("name") or ""),
                    "files": int(lib.get("files") or 0), "bytes": int(lib.get("bytes") or 0),
                    "was_at": was.get(str(lib["id"]), "")})
    return out


def folder_problem(folder: str, library: str) -> str | None:
    """Why *folder* cannot take *library*, or None."""
    path = Path(folder).expanduser()
    if not folder or not path.is_absolute():
        return "Choose a folder by its full path."
    if not path.is_dir():
        return "That folder is not there. Make it, or plug its disk in, first."
    marker = library_id.read_id(path)
    if marker and marker != library:
        return "That folder is another library. Choose an empty folder, or this library's own."
    if marker is None:
        try:
            if any(p.name != ".DS_Store" for p in path.iterdir()):
                return "That folder has other files in it. Choose an empty folder."
        except OSError:
            return "That folder cannot be read."
    return None


class Rebuild:
    """Copies one library back from the Secondary, when somebody asks."""

    def __init__(self, cfg, *, partner: Callable[..., Partner] = Partner,
                 hold: Callable[[], str | None] | None = None,
                 done: Callable[[str], Any] | None = None) -> None:
        self.cfg = cfg
        self.partner = partner
        self.hold = hold or (lambda: None)
        self.done = done
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.live: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {"running": self.running, **self.live}

    def start(self, library: str, folder: str) -> dict[str, Any]:
        with self._lock:
            if role(self.cfg) != PRIMARY:
                raise PartnerError("Only the Primary rebuilds a library.", 409)
            if pairing.load(self.cfg.state_dir) is None or not self.cfg.mugil_partner_url:
                raise PartnerError("This computer is not paired with a Secondary.", 409)
            if self.running:
                raise PartnerError("A library is being rebuilt already.", 409)
            problem = folder_problem(folder, library)
            if problem:
                raise PartnerError(problem, 400)
            self._stop.clear()
            self.live = {"library": library, "folder": str(Path(folder).expanduser()),
                         "started": time.time(), "copied": 0, "copied_bytes": 0,
                         "already": 0, "left_alone": 0, "not_same": 0, "error": ""}
            self._thread = threading.Thread(target=self._run_logged, name="mugil-rebuild",
                                            daemon=True)
            self._thread.start()
        return self.status()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        thread = self._thread
        if join and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

    def _run_logged(self) -> None:
        try:
            self.run(self.live["library"], self.live["folder"])
        except Exception as exc:                                    # noqa: BLE001 - said in status
            log.exception("Rebuilding the library stopped")
            self.live["error"] = str(exc)
        finally:
            self.live["finished"] = time.time()
            self.live.pop("current", None)
            self.live.pop("waiting", None)

    def run(self, library: str, folder: str) -> dict[str, Any]:
        """Copy every original of *library* the Secondary keeps into *folder*."""
        for key in ("copied", "copied_bytes", "already", "left_alone", "not_same"):
            self.live.setdefault(key, 0)
        problem = folder_problem(folder, library)
        if problem:
            raise PartnerError(problem, 400)
        base = Path(folder).expanduser()
        if not library_id.write_marker(base, library):
            raise PartnerError("The folder could not be given the library's id.", 409)
        pair = pairing.load(self.cfg.state_dir) or {}
        partner = self.partner(self.cfg.mugil_partner_url, pair.get("key"), timeout=TIMEOUT)
        after = ""
        while not self._stop.is_set():
            page = self._ask(lambda after=after: partner.request(
                "GET", "/api/mugil/kept", query={"library": library, "after": after,
                                                 "limit": PAGE}))
            if page is None:
                break
            items = [i for i in page.get("items") or [] if isinstance(i, dict)]
            if not items:
                self.live["complete"] = True
                break
            for item in items:
                if self._stop.is_set():
                    break
                self._pause()
                self._one(partner, library, base, item)
            after = str(items[-1].get("path", ""))
        if self.live.get("complete") and self.done is not None:
            self.done(str(base))
        return dict(self.live)

    def _ask(self, call: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        """*call*'s answer; while the Secondary is away, wait for it."""
        wait = AWAY_FIRST
        while not self._stop.is_set():
            try:
                answer = call()
                self.live.pop("waiting", None)
                return answer
            except PartnerError as exc:
                if exc.status not in AWAY:
                    raise
                self.live["waiting"] = AWAY_WORDS
                if self._stop.wait(wait):
                    return None
                wait = min(wait * 2, AWAY_LONGEST)
        return None

    def _pause(self) -> None:
        while True:
            why = self.hold()
            if not why:
                self.live.pop("waiting", None)
                return
            self.live["waiting"] = why
            if self._stop.wait(30):
                return

    def _one(self, partner: Partner, library: str, base: Path, item: dict[str, Any]) -> None:
        rel = clean_path(str(item.get("path", "")))
        if rel is None:
            return
        target = base / rel
        self.live["current"] = rel
        size, algo, digest = int(item.get("size") or 0), str(item.get("algo", "")), \
            str(item.get("digest", ""))
        mtime = float(item.get("mtime") or 0)
        if target.exists() or target.is_symlink():
            # Never replaced: the same file counts as copied before, anything
            # else stays exactly as it is.
            try:
                same = (target.is_file() and not target.is_symlink()
                        and target.stat().st_size == size and _fingerprint(target, algo) == digest)
            except OSError:
                same = False
            self.live["already" if same else "left_alone"] += 1
            return
        sum_of = {"now": hasher(algo)}
        if sum_of["now"] is None:
            self.live["not_same"] += 1
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.ninaivu-partial-{secrets.token_hex(4)}")
        try:
            with open(partial, "xb") as handle:
                def write(chunk: bytes, handle=handle) -> None:
                    sum_of["now"].update(chunk)
                    handle.write(chunk)
                got = None
                wait = AWAY_FIRST
                while got is None and not self._stop.is_set():
                    try:
                        got = partner.download("/api/mugil/originals",
                                               query={"library": library, "path": rel},
                                               write=write)
                    except PartnerError as exc:
                        if exc.status not in AWAY:
                            log.info("The Secondary did not send %s: %s", rel, exc)
                            self.live["not_same"] += 1
                            return
                        # Away mid-file: start this one again once it is back.
                        handle.seek(0)
                        handle.truncate()
                        sum_of["now"] = hasher(algo)
                        self.live["waiting"] = AWAY_WORDS
                        if self._stop.wait(wait):
                            return
                        wait = min(wait * 2, AWAY_LONGEST)
                if got is None:
                    return
                self.live.pop("waiting", None)
                handle.flush()
                os.fsync(handle.fileno())
            if got != size or sum_of["now"].hexdigest() != digest:
                log.warning("%s did not arrive as the Secondary keeps it; not kept", rel)
                self.live["not_same"] += 1
                return
            os.chmod(partial, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            os.utime(partial, (mtime, mtime))
            if target.exists():
                self.live["left_alone"] += 1
                return
            os.replace(partial, target)
        except OSError as exc:
            log.warning("Could not keep %s here: %s", rel, exc)
            self.live["error"] = str(exc)
            self._stop.set()
            return
        finally:
            partial.unlink(missing_ok=True)
        self.live["copied"] += 1
        self.live["copied_bytes"] += size


def _fingerprint(path: Path, algo: str) -> str | None:
    from .free import fingerprint                                   # noqa: PLC0415
    return fingerprint(path, algo)


__all__ = ["Rebuild", "libraries", "folder_problem", "PAGE"]
