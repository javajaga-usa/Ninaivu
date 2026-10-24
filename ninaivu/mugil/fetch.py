"""On the Primary: sending for an original that is not on this computer.

With two computers every original is kept on the Secondary, and the Primary
may be without one: its library disk unplugged, or (a later step, only on
the household's word) the original let go to save room. Someone opening,
downloading or editing it should not have to know. So when the file is not
where the index says, and the Secondary confirmed it (``mugil-sent.db``),
the Primary sends for it.

It arrives under a temporary name in ``fetched/`` in the state folder, is
fingerprinted as it is written, and is kept only when the fingerprint and
size are the ones recorded when it was sent; then it is given its modified
time, made read-only (a fetched original is to be looked at, not changed
behind the library's back) and named as the original was. The folder is a
cache: the least recently used files go once it holds more than
:data:`MOST_BYTES`, and nothing in the library is touched.

One computer, or a Primary not paired, never sends for anything: the file
is missing, as it always was.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any

from . import PRIMARY, pairing, role
from .courier import SENT, Partner, PartnerError, sent_record
from .hold import hasher

log = logging.getLogger(__name__)

FOLDER = "fetched"
#: The most the fetched originals may take before the oldest-used go.
MOST_BYTES = 20 * 1024 ** 3
#: How long to wait on the Secondary for each piece of a file.
TIMEOUT = 60.0

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(digest: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(digest, threading.Lock())


def folder(state_dir: str | Path) -> Path:
    return Path(state_dir) / FOLDER


def record_of(state_dir: str | Path, root: str, rel: str) -> dict[str, Any] | None:
    """What the Secondary confirmed of *rel* in *root*, with its library."""
    if not (Path(state_dir) / SENT).exists():
        return None
    with sent_record(state_dir) as conn:
        row = conn.execute(
            "SELECT s.size, s.mtime, s.algo, s.digest, l.library FROM sent s "
            "JOIN libraries l ON l.root = s.root WHERE s.root=? AND s.rel_path=?",
            (str(root), str(rel))).fetchone()
    return dict(row) if row else None


def cached(state_dir: str | Path, digest: str, name: str, size: int) -> Path | None:
    path = folder(state_dir) / digest[:2] / digest / name
    try:
        if path.is_file() and path.stat().st_size == size:
            now = time.time()
            # Used now: the last to go when the folder is trimmed.
            os.utime(path.parent, (now, now))
            return path
    except OSError:
        pass
    return None


def original(cfg, root: str, rel: str, *, partner=None) -> Path | None:
    """A local path to *rel* of *root* sent for from the Secondary, or None."""
    if role(cfg) != PRIMARY:
        return None
    pair = pairing.load(cfg.state_dir)
    if pair is None or not getattr(cfg, "mugil_partner_url", ""):
        return None
    found = record_of(cfg.state_dir, root, rel)
    if found is None:
        return None
    name = Path(rel).name
    digest, size = found["digest"], int(found["size"])
    with _lock_for(digest):
        hit = cached(cfg.state_dir, digest, name, size)
        if hit is not None:
            return hit
        fresh = hasher(found["algo"])
        if fresh is None:
            return None
        home = folder(cfg.state_dir) / digest[:2] / digest
        home.mkdir(parents=True, exist_ok=True)
        partial = home / f".{name}.ninaivu-partial-{secrets.token_hex(4)}"
        try:
            with open(partial, "xb") as handle:
                def write(chunk: bytes) -> None:
                    fresh.update(chunk)
                    handle.write(chunk)
                got = (partner or Partner)(cfg.mugil_partner_url, pair.get("key"), timeout=TIMEOUT).download(
                    "/api/mugil/originals", query={"library": found["library"], "path": rel},
                    write=write)
                handle.flush()
                os.fsync(handle.fileno())
            if got != size or fresh.hexdigest() != digest:
                log.warning("The original sent for, %s, is not the one sent; not kept", rel)
                return None
            os.utime(partial, (float(found["mtime"]), float(found["mtime"])))
            os.chmod(partial, stat.S_IRUSR | stat.S_IRGRP)
            target = home / name
            os.replace(partial, target)
        except PartnerError as exc:
            log.info("Could not send for %s from the Secondary: %s", rel, exc)
            return None
        except OSError as exc:
            log.warning("Could not keep %s sent for from the Secondary: %s", rel, exc)
            return None
        finally:
            partial.unlink(missing_ok=True)
    trim(cfg.state_dir, keep=home)
    return target


def trim(state_dir: str | Path, *, most: int = MOST_BYTES, keep: Path | None = None) -> int:
    """Let the least recently used fetched originals go past *most*; bytes freed."""
    base = folder(state_dir)
    entries = []
    total = 0
    try:
        for first in base.iterdir():
            for home in first.iterdir():
                size = sum(f.stat().st_size for f in home.iterdir() if f.is_file())
                entries.append((home.stat().st_mtime, size, home))
                total += size
    except OSError:
        return 0
    freed = 0
    for _used, size, home in sorted(entries):
        if total - freed <= most:
            break
        if keep is not None and home == keep:
            continue
        for f in home.iterdir():
            os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)
        shutil.rmtree(home, ignore_errors=True)
        freed += size
    return freed


def summary(state_dir: str | Path) -> dict[str, int]:
    files = size = 0
    for path in folder(state_dir).rglob("*"):
        try:
            if path.is_file() and ".ninaivu-partial-" not in path.name:
                files += 1
                size += path.stat().st_size
        except OSError:
            continue
    return {"files": files, "bytes": size}


__all__ = ["original", "record_of", "trim", "summary", "MOST_BYTES"]
