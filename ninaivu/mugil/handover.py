"""The family app while the Primary is off: its read-only copy on the Secondary.

The Primary is not always on; the Secondary is. With ``mugil_family_copy``
on, the Primary sends the Secondary a *hand-over*: a slimmed snapshot of the
index (:func:`slim`), the thumbnails, faces, profile pictures, stories and
films, and ``replica.json`` last, over the pairing with every request signed,
into ``mugil-handover/`` in the Secondary's state folder. There the
Secondary's own Ninaivu serves it read-only (mugil/family_view.py).

(Until 4.0 the same hand-over could also be written into a shared folder for
a Raspberry Pi to serve. That way is retired; this one is what is left of it,
and the pieces both used live here now.)

**On the Primary** (:class:`Handover`): the index snapshot is made and
slimmed; each folder sends only the files that changed since they were last
sent (the record, ``replica-sent.db``, keyed by destination), and removes on
the Secondary what is gone here; ``replica.json`` goes last. Each file is
fingerprinted and is kept by the Secondary only when the bytes match, written
under a temporary name and renamed, so a hand-over half sent leaves the one
before whole.

**On the Secondary** (:func:`receive`): the files land in
``mugil-handover/``, and the Secondary's own Ninaivu serves them on its
family app while the Primary does not answer (mugil/family_view.py). The
photographs themselves are the originals the Secondary keeps; each library's
paths are re-rooted onto its folder there, through the Secondary's own record
of which library is in which folder (mugil/hold.py). The old way,
``ninaivu --replica-of`` as a service of its own with the originals bind
mounted at the Primary's paths, still works.

Nothing here changes the library, or runs with one computer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..storage import roots as roots_kit
from . import PRIMARY, pairing, role
from .courier import AWAY, Partner, PartnerError
from .hold import clean_path, hasher

log = logging.getLogger(__name__)

#: In the Secondary's state folder: where the hand-over is kept.
FOLDER = "mugil-handover"
#: How often the Primary looks whether a hand-over is due, and the least time
#: between two.
LOOK_EVERY = 5 * 60.0
EVERY = 60 * 60.0
#: In the Primary's state folder: when the last hand-over was sent, and how.
RECORD = "mugil-handover.json"

_INDEX = re.compile(r"^index-\d+\.db$")


#: Beside the index on the Primary: which files were sent to which destination.
SENT = "replica-sent.db"

#: The settings the family view takes from the Primary: what the family sees
#: and how, and nothing about this machine (its ports, its disks, its
#: backups, its models) and no secret. A list of what goes rather than of
#: what stays, so a setting added later stays on the Primary until somebody
#: decides it belongs here.
SETTINGS = (
    "roots", "active_root", "lock_roots", "house_name", "open_browsing",
    "lock_after_minutes", "thumb_sizes", "thumb_format", "thumb_quality",
    "video_thumbs", "page_size", "max_page_size", "nsfw_filter", "hide_screens",
    "strip_location", "home_lat", "home_lon", "home_radius_m", "index_hidden",
    "southern_hemisphere", "date_phrase_search", "phrase_search", "map_tiles",
    "place_names", "faces_enabled", "ocr_enabled", "min_media_bytes",
    "ignore_dirs", "follow_symlinks",
)

def slim(index: Path) -> None:
    """Take out of a snapshot what must not leave the Primary, and what the
    family view has no use for.

    Kept: everything the family app shows — photographs, faces and who they
    are, albums, visibility, dates, captions, stories — and the accounts, so
    the family signs in on the Pi with the passwords and PINs they use on the
    mini. Taken out: sign-in sessions (keys to the Primary), the sign-in record
    and counters (keyed by whatever was typed, a whole share link included),
    share and "who is this?" tokens (each one *is* its link), the handover
    code's hash, upload resume addresses, and the image-search vectors, which
    are half the index and need a model the Pi does not run.
    """
    with closing(sqlite3.connect(str(index))) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("sessions", "audit", "auth_limits", "shares", "embeddings",
                      "ask_answers", "ask_faces", "ask_questions"):
            if table in tables:
                conn.execute(f"DELETE FROM {table}")
        if "handover_plan" in tables:
            conn.execute("UPDATE handover_plan SET code_hash=''")
        if "cloud_uploads" in tables:
            conn.execute("UPDATE cloud_uploads SET resume_url=''")
        if "pending_uploads" in tables:
            # Their bytes are on the Primary, waiting to be looked at; not in
            # the library, so not among the originals the Secondary keeps.
            conn.execute("DELETE FROM pending_uploads WHERE status='pending'")
        conn.commit()
        conn.execute("VACUUM")


def schema_version(index: Path) -> int:
    with closing(sqlite3.connect(str(index))) as conn:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error:
            return 0
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def family_settings(cfg) -> dict[str, Any]:
    """The settings in :data:`SETTINGS`, as JSON holds them."""
    out: dict[str, Any] = {}
    for key in SETTINGS:
        value = getattr(cfg, key, None)
        if isinstance(value, (set, frozenset)):
            value = sorted(value)
        elif isinstance(value, tuple):
            value = list(value)
        out[key] = value
    return out


def library_ids(cfg) -> dict[str, str]:
    """The marker id of each library folder, from ``library-ids.json``."""
    try:
        known = json.loads((Path(cfg.state_dir) / roots_kit.KNOWN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(known, dict):
        return {}
    return {root: known[root] for root in cfg.library_roots
            if isinstance(known.get(root), str)}


def open_sent(state_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(state_dir) / SENT), timeout=30.0)
    conn.execute("""CREATE TABLE IF NOT EXISTS sent (
        dest TEXT NOT NULL, folder TEXT NOT NULL, path TEXT NOT NULL,
        size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
        PRIMARY KEY (dest, folder, path))""")
    return conn


def listing(folder: Path) -> dict[str, tuple[int, int]]:
    """Every file under *folder*: relative path -> (size, mtime in ns)."""
    found: dict[str, tuple[int, int]] = {}
    if not folder.is_dir():
        return found
    stack = [folder]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") and entry.name.endswith(".part"):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    rel = Path(entry.path).relative_to(folder).as_posix()
                    found[rel] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return found


def wanted(cfg) -> bool:
    return (role(cfg) == PRIMARY and bool(getattr(cfg, "mugil_family_copy", False))
            and pairing.load(cfg.state_dir) is not None and bool(cfg.mugil_partner_url))


def allowed_path(rel: str) -> str | None:
    """*rel* when it is a path a hand-over may hold, else None."""
    from ..replica import FOLDERS, MANIFEST                      # noqa: PLC0415
    clean = clean_path(rel)
    if clean is None:
        return None
    first = clean.split("/", 1)[0]
    if clean in (MANIFEST,) or _INDEX.match(clean) or (first in FOLDERS and "/" in clean):
        return clean
    return None


def _fingerprint(path: Path) -> tuple[str, str, int]:
    fresh = hasher("blake3") or hasher("blake2b")
    algo = "blake3" if hasher("blake3") is not None else "blake2b"
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            fresh.update(chunk)
    return algo, fresh.hexdigest(), size


class Handover:
    """Sends the family app's copy to the Secondary, by itself, now and then."""

    def __init__(self, cfg, *, partner: Callable[..., Partner] = Partner,
                 hold: Callable[[], str | None] | None = None) -> None:
        self.cfg = cfg
        self.partner = partner
        self.hold = hold or (lambda: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.live: dict[str, Any] = {}

    # -- record ------------------------------------------------------------
    def last(self) -> dict[str, Any]:
        try:
            data = json.loads((Path(self.cfg.state_dir) / RECORD).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, **fields: Any) -> None:
        path = Path(self.cfg.state_dir) / RECORD
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({**self.last(), **fields}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("could not note the hand-over: %s", exc)

    def _stamp(self) -> list[int | str]:
        """What changes whenever the index or the family's settings do, read
        without opening the index.

        The settings travel in the hand-over's manifest (:func:`family_settings`),
        not in the index: one changed on its own (the content filter, open
        browsing) writes nothing to the index, and the Secondary kept the old
        one until something else changed the index (audit A-04, which the Pi's
        hand-over had and this one did not).
        """
        out: list[int | str] = []
        for suffix in ("", "-wal"):
            try:
                st = os.stat(f"{self.cfg.db_path}{suffix}")
                out += [st.st_mtime_ns, st.st_size]
            except OSError:
                out += [0, 0]
        settings = json.dumps(family_settings(self.cfg), sort_keys=True, default=str)
        out.append(hashlib.sha256(settings.encode("utf-8")).hexdigest()[:16])
        return out

    def due(self, now: float | None = None) -> bool:
        if not wanted(self.cfg):
            return False
        now = time.time() if now is None else now
        last = self.last()
        if now - float(last.get("at") or 0) < EVERY:
            return False
        return last.get("stamp") != self._stamp() or bool(last.get("error"))

    def status(self) -> dict[str, Any]:
        last = self.last()
        return {"on": bool(getattr(self.cfg, "mugil_family_copy", False)),
                "running": self._thread is not None and self._thread.is_alive(),
                "at": last.get("at"), "error": last.get("error", ""),
                "generation": last.get("generation"), **self.live}

    # -- running -------------------------------------------------------------
    def keep(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._keep, name="mugil-handover", daemon=True)
        self._thread.start()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._thread = None

    def _keep(self) -> None:
        while not self._stop.wait(LOOK_EVERY):
            try:
                if self.due():
                    self.run()
            except Exception:                                    # noqa: BLE001 - keep looking
                log.exception("The family app's copy could not be sent")

    def _pause(self) -> None:
        while True:
            why = self.hold()
            if not why:
                self.live.pop("waiting", None)
                return
            self.live["waiting"] = why
            if self._stop.wait(30):
                raise _Stopped()

    def run(self) -> dict[str, Any] | None:
        """Send one hand-over now; its manifest, or None when it did not go."""
        from ..replica import FOLDERS, FORMAT, MANIFEST          # noqa: PLC0415
        from ..storage import backup                             # noqa: PLC0415
        from ..utils.files import sha256_file                    # noqa: PLC0415
        pair = pairing.load(self.cfg.state_dir) or {}
        partner = self.partner(self.cfg.mugil_partner_url, pair.get("key"))
        stamp = self._stamp()
        state = Path(self.cfg.state_dir)
        dest = f"mugil:{pair.get('partner_address') or self.cfg.mugil_partner_url}"
        generation = time.time_ns()
        name = f"index-{generation}.db"
        try:
            with tempfile.TemporaryDirectory(prefix="ninaivu_handover_", dir=state) as tmp:
                staged = Path(tmp) / "index.db"
                if not backup.hot_copy(Path(self.cfg.db_path), staged):
                    raise OSError("the index could not be snapshotted")
                slim(staged)
                digest = sha256_file(staged)
                size = staged.stat().st_size
                schema = schema_version(staged)
                self._send(partner, staged, name)
            counts: dict[str, dict[str, int]] = {}
            with closing(open_sent(state)) as sent:
                for folder in FOLDERS:
                    counts[folder] = self._send_folder(partner, sent, dest, state / folder, folder)
            manifest = {
                "format": FORMAT, "generation": generation, "made_at": time.time(),
                "version": __version__, "schema_version": schema,
                "index": {"file": name, "size": size, "sha256": digest},
                "libraries": library_ids(self.cfg),
                # Where the family app answers here, so the Secondary can send
                # visitors to it while this computer is awake (family_view.py).
                "family_port": int(getattr(self.cfg, "port", 80) or 80),
                "settings": family_settings(self.cfg), "folders": counts,
            }
            with tempfile.NamedTemporaryFile("w", dir=state, suffix=".json", delete=False,
                                             encoding="utf-8") as out:
                json.dump(manifest, out, indent=2)
            try:
                self._send(partner, Path(out.name), MANIFEST)
            finally:
                os.unlink(out.name)
        except _Stopped:
            self._remember(error="", stopped_at=time.time())
            return None
        except (PartnerError, OSError) as exc:
            away = isinstance(exc, PartnerError) and exc.status in AWAY
            log.info("The family app's copy was not sent: %s", exc)
            # Away is not a fault: it goes next time, as the originals do.
            self._remember(error="" if away else str(exc), failed_at=time.time(), away=away)
            return None
        finally:
            self.live = {}
        self._remember(at=time.time(), stamp=stamp, error="", away=False,
                       generation=generation, index_bytes=size)
        return manifest

    def _send(self, partner: Partner, path: Path, rel: str) -> None:
        if self._stop.is_set():
            raise _Stopped()
        self._pause()
        algo, digest, size = _fingerprint(path)
        with open(path, "rb") as handle:
            partner.request("PUT", "/api/mugil/handover", query={"path": rel}, body=handle,
                            length=size, digest=f"{algo}:{digest}:{size}",
                            headers={"Content-Type": "application/octet-stream"})

    def _send_folder(self, partner: Partner, sent, dest: str, source: Path,
                     folder: str) -> dict[str, int]:
        here = listing(source)
        before = {row[0]: (row[1], row[2]) for row in sent.execute(
            "SELECT path, size, mtime_ns FROM sent WHERE dest=? AND folder=?", (dest, folder))}
        copied = 0
        for rel, stamp in here.items():
            if before.get(rel) == stamp:
                continue
            try:
                self._send(partner, source / rel, f"{folder}/{rel}")
            except FileNotFoundError:
                continue
            except PartnerError as exc:
                if exc.status in (*AWAY, 401, 403, 404, 507):
                    sent.commit()
                    raise
                log.warning("The Secondary did not keep %s/%s: %s", folder, rel, exc)
                continue
            sent.execute("INSERT OR REPLACE INTO sent(dest, folder, path, size, mtime_ns) "
                         "VALUES (?,?,?,?,?)", (dest, folder, rel, stamp[0], stamp[1]))
            copied += 1
            self.live = {"folder": folder, "sent": copied}
            if copied % 500 == 0:
                sent.commit()
        gone = sorted(before.keys() - here.keys())
        for start in range(0, len(gone), 1000):
            chunk = gone[start:start + 1000]
            partner.request("POST", "/api/mugil/handover/remove",
                            body={"paths": [f"{folder}/{rel}" for rel in chunk]})
            sent.executemany("DELETE FROM sent WHERE dest=? AND folder=? AND path=?",
                             [(dest, folder, rel) for rel in chunk])
        sent.commit()
        return {"copied": copied, "removed": len(gone), "files": len(here)}


class _Stopped(Exception):
    pass


# -- on the Secondary -----------------------------------------------------------------

def folder(state_dir: str | Path) -> Path:
    return Path(state_dir) / FOLDER


def receive(state_dir: str | Path, rel: str, *, algo: str, digest: str, size: int,
            body) -> str:
    """Keep one file of a hand-over, if it is the one described. Raises
    ValueError naming what was wrong."""
    from ..replica import MANIFEST                               # noqa: PLC0415
    clean = allowed_path(rel)
    if clean is None:
        raise ValueError("path")
    fresh = hasher(algo)
    if fresh is None:
        raise ValueError("algo")
    base = folder(state_dir)
    target = base / clean
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    got = 0
    try:
        with open(partial, "xb") as handle:
            while True:
                chunk = body.read(min(1024 * 1024, size - got + 1))
                if not chunk:
                    break
                got += len(chunk)
                if got > size:
                    break
                fresh.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if got != size or fresh.hexdigest() != digest:
            raise ValueError("mismatch")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    if clean == MANIFEST:
        _prune(base)
    return clean


def remove(state_dir: str | Path, paths: list[str]) -> int:
    base = folder(state_dir)
    gone = 0
    for rel in paths:
        clean = allowed_path(str(rel))
        if clean is None or "/" not in clean:
            continue                          # only files in the folders, never the index
        try:
            (base / clean).unlink()
            gone += 1
        except OSError:
            continue
    return gone


def _prune(base: Path) -> None:
    """Keep the index the manifest names and the newest one before it."""
    from ..replica import MANIFEST                               # noqa: PLC0415
    try:
        named = json.loads((base / MANIFEST).read_text(encoding="utf-8"))["index"]["file"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    snapshots = sorted(base.glob("index-*.db"), key=lambda p: p.name)
    keep = {named}
    older = [p.name for p in snapshots if p.name < named]
    if older:
        keep.add(older[-1])
    for path in snapshots:
        if path.name not in keep:
            path.unlink(missing_ok=True)


def summary(state_dir: str | Path) -> dict[str, Any] | None:
    """On the Secondary: the hand-over it holds, or None."""
    from ..replica import MANIFEST                               # noqa: PLC0415
    try:
        data = json.loads((folder(state_dir) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"folder": str(folder(state_dir)), "made_at": data.get("made_at"),
            "generation": data.get("generation"), "version": data.get("version")}


__all__ = ["Handover", "receive", "remove", "summary", "allowed_path", "wanted", "FOLDER",
           "SETTINGS", "SENT", "slim", "schema_version", "family_settings", "library_ids",
           "open_sent", "listing"]
