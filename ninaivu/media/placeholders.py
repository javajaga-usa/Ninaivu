"""Blurred previews: what a tile shows before its thumbnail arrives.

A fast scroll on a phone far from home outruns the thumbnails: each one is a
round trip of a third of a second or more, the browser fetches six at a time,
and a flick passes a hundred tiles a second. Before this a tile sat on its one
colour until its picture came, which on that link was most of the screen for
most of the scroll. A preview here is a photograph's shapes and colours in
about two dozen bytes (the ThumbHash format, by Evan Wallace, MIT licence:
https://github.com/evanw/thumbhash), so the previews for a few hundred tiles
come in one request of a few kilobytes, well ahead of the scroll, and the
browser draws each one at once, blurred, until its picture replaces it.

Made from the smallest thumbnail, kept beside the thumbnails in a small
SQLite file of their own (``placeholders.db``): nothing in it that cannot be
made again, so it is neither in the index nor in a backup of it. Each is
stamped with the thumbnail's version, so a turn or a re-index makes it again.

Only opaque pictures are described (thumbnails have no transparency), which
is the part of the format the gallery's decoder (static/js/placeholder.js)
reads.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np

log = logging.getLogger(__name__)

#: The picture a preview is made from is first made this small: past a few
#: dozen pixels nothing more survives into seven coefficients across.
SIDE = 32

#: How many coefficients of brightness at most, along the longer side.
L_LIMIT = 7

FILE_NAME = "placeholders.db"


def _round(value: float) -> int:
    # Half away from nothing upward, as JavaScript's Math.round: the format is
    # defined there, and Python's own round() sends 0.5 to 0.
    return int(math.floor(value + 0.5))


def _channel(values: np.ndarray, nx: int, ny: int) -> tuple[float, list[float], float]:
    """The DCT coefficients of one channel, in the format's order: by row
    of the lower triangle (cx * ny < nx * (ny - cy)), the first being the
    average, the rest scaled into 0..1 by the largest."""
    h, w = values.shape
    fx = np.cos(np.pi / w * np.outer(np.arange(nx), np.arange(w) + 0.5))   # nx × w
    fy = np.cos(np.pi / h * np.outer(np.arange(ny), np.arange(h) + 0.5))   # ny × h
    coefficients = fy @ values @ fx.T / (w * h)                              # ny × nx
    dc = 0.0
    ac: list[float] = []
    scale = 0.0
    for cy in range(ny):
        cx = 0
        while cx * ny < nx * (ny - cy):
            f = float(coefficients[cy, cx])
            if cx or cy:
                ac.append(f)
                scale = max(scale, abs(f))
            else:
                dc = f
            cx += 1
    if scale:
        ac = [0.5 + 0.5 / scale * f for f in ac]
    return dc, ac, scale


def encode_rgb(pixels: np.ndarray) -> bytes:
    """The preview of an image given as an ``h × w × 3`` array of 0..255."""
    h, w = pixels.shape[:2]
    if not w or not h:
        raise ValueError("an empty picture has no preview")
    rgb = pixels.astype(np.float64) / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lum = (r + g + b) / 3
    p = (r + g) / 2 - b
    q = r - g
    lx = max(1, _round(L_LIMIT * w / max(w, h)))
    ly = max(1, _round(L_LIMIT * h / max(w, h)))
    l_dc, l_ac, l_scale = _channel(lum, max(3, lx), max(3, ly))
    p_dc, p_ac, p_scale = _channel(p, 3, 3)
    q_dc, q_ac, q_scale = _channel(q, 3, 3)

    landscape = w > h
    header24 = (_round(63 * l_dc) | (_round(31.5 + 31.5 * p_dc) << 6)
                | (_round(31.5 + 31.5 * q_dc) << 12) | (_round(31 * l_scale) << 18))
    header16 = ((ly if landscape else lx) | (_round(63 * p_scale) << 3)
                | (_round(63 * q_scale) << 9) | (int(landscape) << 15))
    out = bytearray([header24 & 255, (header24 >> 8) & 255, header24 >> 16,
                     header16 & 255, header16 >> 8])
    index = 0
    for f in (*l_ac, *p_ac, *q_ac):
        at = 5 + (index >> 1)
        if at >= len(out):
            out.append(0)
        out[at] |= _round(15 * f) << ((index & 1) * 4)
        index += 1
    return bytes(out)


def encode_file(path: Path) -> bytes:
    """The preview of the picture in *path* (a thumbnail)."""
    from PIL import Image                                   # noqa: PLC0415

    with Image.open(path) as image:
        image.draft("RGB", (SIDE * 2, SIDE * 2))
        small = image.convert("RGB")
        small.thumbnail((SIDE, SIDE), Image.Resampling.BILINEAR)
        return encode_rgb(np.asarray(small))


class Store:
    """The previews made so far, by asset id, each with the thumbnail version
    it was made from."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / FILE_NAME
        self._local = threading.local()
        self._ready = False
        self._lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with self._lock:
                if not self._ready:
                    conn.execute("CREATE TABLE IF NOT EXISTS placeholders ("
                                 "asset_id INTEGER PRIMARY KEY, stamp TEXT NOT NULL, "
                                 "hash BLOB NOT NULL)")
                    conn.commit()
                    self._ready = True
            self._local.conn = conn
        return conn

    def get(self, wanted: dict[int, str]) -> dict[int, bytes]:
        """The previews held for *wanted* (asset id → stamp) whose stamp still
        matches."""
        if not wanted:
            return {}
        conn = self._db()
        found: dict[int, bytes] = {}
        ids = list(wanted)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for asset_id, stamp, blob in conn.execute(
                    f"SELECT asset_id, stamp, hash FROM placeholders WHERE asset_id IN ({marks})",
                    chunk):
                if wanted.get(asset_id) == stamp:
                    found[asset_id] = bytes(blob)
        return found

    def put(self, made: Iterable[tuple[int, str, bytes]]) -> None:
        rows = list(made)
        if not rows:
            return
        conn = self._db()
        conn.executemany("INSERT OR REPLACE INTO placeholders(asset_id, stamp, hash) "
                         "VALUES (?, ?, ?)", rows)
        conn.commit()


def stamp(row: dict[str, Any], version: str) -> str:
    """What a preview was made from: the thumbnail's name and version."""
    return f"{row.get('thumb') or ''}|{version}"


def previews(store: Store, rows: list[dict[str, Any]], *, source, version) -> dict[int, bytes]:
    """The preview of every row that has a thumbnail, made now for those that
    have none yet (or an out-of-date one). *source(row)* is the path of the
    thumbnail to make it from; *version(row)* its version."""
    wanted = {int(r["id"]): stamp(r, version(r)) for r in rows if r.get("thumb")}
    found = store.get(wanted)
    made = []
    # Rows sharing one thumbnail (a made-up library; copies) are read once.
    by_name: dict[str, bytes] = {}
    for row in rows:
        asset_id = int(row["id"])
        if asset_id not in wanted or asset_id in found:
            continue
        path = source(row)
        blob = by_name.get(str(path))
        if blob is None:
            try:
                blob = encode_file(path)
            except Exception:                       # noqa: BLE001 - no preview is fine
                continue
            by_name[str(path)] = blob
        found[asset_id] = blob
        made.append((asset_id, wanted[asset_id], blob))
    try:
        store.put(made)
    except sqlite3.Error as error:                  # a full disk: still answered
        log.debug("previews not kept: %s", error)
    return found


def pack(ids: list[int], found: dict[int, bytes]) -> bytes:
    """The answer to a request for *ids*: for each, in order, one byte of
    length and that many bytes of preview (0 for none)."""
    out = bytearray()
    for asset_id in ids:
        blob = found.get(asset_id, b"")[:255]
        out.append(len(blob))
        out += blob
    return bytes(out)


class Filler:
    """Makes the previews of the whole library, a little at a time, once the
    gallery has first asked for some: the first fast scroll through a part of
    the library nobody has opened since then then finds them made already.

    Gentle: a batch, then a rest three times as long as the batch took, so it
    takes at most a quarter of one core, and only while the server is up."""

    BATCH = 200

    def __init__(self, store: Store, *, rows, source, version):
        self.store = store
        self.rows = rows            # rows(after_id, limit) -> list of dict rows
        self.source = source
        self.version = version
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="ninaivu-previews", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        after = 0
        try:
            while not self.stop.is_set():
                batch = self.rows(after, self.BATCH)
                if not batch:
                    return
                after = int(batch[-1]["id"])
                started = time.monotonic()
                previews(self.store, batch, source=self.source, version=self.version)
                took = time.monotonic() - started
                self.stop.wait(max(0.05, took * 3))
        except Exception:                           # noqa: BLE001 - a cache, never fatal
            log.debug("making previews stopped", exc_info=True)
