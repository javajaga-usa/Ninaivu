"""A gzip stream compressed on several cores at once.

The update copy (``storage/backup.py``) spent most of its eight minutes in
``tarfile``'s gzip at level 9, on one core (audit of 3 November 2026, open
item 1). Lowering the level would have made it faster and every bundle
bigger; this keeps level 9 and the size, and uses the other cores.

It is what pigz does. The input is cut into chunks; each is deflated on its
own thread, primed with the last 32 KiB of the chunk before it so it finds
the same matches a single pass would; every chunk but the last ends on a sync
flush, the last on a finish. Those pieces, joined in order, are one ordinary
deflate stream, so the file is **one gzip member** with one CRC-32 and one
length, byte-for-byte readable by anything that reads gzip. That matters:
``tarfile``'s streaming reader (``r|gz``, which ``backup.extract_verified``
uses) stops at the end of the first member, so gzip files simply joined end to
end would have read as a bundle cut short — and so would they in every older
Ninaivu asked to restore one.

zlib lets go of the GIL while it deflates, so plain threads are enough.
"""

from __future__ import annotations

import collections
import os
import struct
import time
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from typing import BinaryIO

__all__ = ["ParallelGzipWriter", "CHUNK", "default_workers"]

#: Input deflated as one piece. Big enough that the sync flush between pieces
#: (five bytes) and the lost matches across the seam are lost in the noise,
#: small enough that a few in flight per thread stay a few megabytes.
CHUNK = 1 << 20

#: The window deflate looks back over, and so what each piece is primed with.
_WINDOW = 32 * 1024


def default_workers() -> int:
    """Threads to deflate on: every core, up to eight."""
    return max(1, min(8, os.cpu_count() or 1))


def _deflate(data: bytes, prime: bytes, level: int, last: bool) -> bytes:
    if prime:
        packer = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS, 9,
                                  zlib.Z_DEFAULT_STRATEGY, prime)
    else:
        packer = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS, 9)
    return packer.compress(data) + packer.flush(zlib.Z_FINISH if last else zlib.Z_SYNC_FLUSH)


class ParallelGzipWriter:
    """Write-only file object: what is written to it goes to *fileobj* as gzip.

    Use it as a context manager, or call :meth:`close`; *fileobj* itself is
    left open. Leaving the ``with`` block on an exception writes no trailer,
    so what was written is not mistaken for a whole file.
    """

    def __init__(self, fileobj: BinaryIO, level: int = 9, *, workers: int | None = None,
                 chunk: int = CHUNK, mtime: float | None = None) -> None:
        if not 0 <= level <= 9:
            # A programmer's mistake, never the household's: not in the
            # catalogue of messages to translate (tools/server_messages.py).
            raise ValueError(f"gzip level must be 0-9, not {level!r}")
        self._out = fileobj
        self._level = level
        self._chunk = max(_WINDOW, int(chunk))
        self._workers = max(1, int(workers or default_workers()))
        self._pool = ThreadPoolExecutor(max_workers=self._workers,
                                        thread_name_prefix="ninaivu-gzip")
        # In order, oldest first. Bounded, so a fast reader cannot pile the
        # whole input up in memory waiting for slow threads.
        self._pending: collections.deque[Future[bytes]] = collections.deque()
        self._most_pending = self._workers * 2
        self._buffer = bytearray()
        self._prime = b""
        self._crc = 0
        self._size = 0
        self._closed = False
        # The same ten bytes gzip.GzipFile writes, without a name: XFL says
        # "slowest" at 9 and "fastest" at 1, OS 255 is "unknown".
        stamp = int(time.time() if mtime is None else mtime) & 0xFFFFFFFF
        xfl = 2 if level == 9 else 4 if level == 1 else 0
        self._out.write(b"\x1f\x8b\x08\x00" + struct.pack("<I", stamp) + bytes((xfl, 255)))

    # -- file object ------------------------------------------------------
    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        if self._closed:
            raise ValueError(f"write to a closed {type(self).__name__}")
        view = memoryview(data).cast("B")
        self._buffer += view
        while len(self._buffer) >= self._chunk:
            piece = bytes(self._buffer[:self._chunk])
            del self._buffer[:self._chunk]
            self._send(piece, last=False)
        return len(view)

    def flush(self) -> None:
        """Nothing to do: a piece goes out once it is whole, and the rest at
        :meth:`close`. Here because ``tarfile`` calls it."""

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._send(bytes(self._buffer), last=True)
            self._buffer.clear()
            while self._pending:
                self._out.write(self._pending.popleft().result())
            self._out.write(struct.pack("<II", self._crc & 0xFFFFFFFF,
                                        self._size & 0xFFFFFFFF))
        finally:
            self._abandon()

    def __enter__(self) -> ParallelGzipWriter:
        return self

    def __exit__(self, kind, value, traceback) -> None:
        if kind is None:
            self.close()
        else:
            self._abandon()

    # -- inside -----------------------------------------------------------
    def _send(self, piece: bytes, last: bool) -> None:
        self._pending.append(self._pool.submit(_deflate, piece, self._prime,
                                               self._level, last))
        self._crc = zlib.crc32(piece, self._crc)
        self._size += len(piece)
        self._prime = (self._prime + piece)[-_WINDOW:] if len(piece) < _WINDOW \
            else piece[-_WINDOW:]
        # Write out what is done, in order, and wait for the oldest once too
        # many are in flight.
        while self._pending and (self._pending[0].done()
                                 or len(self._pending) > self._most_pending):
            self._out.write(self._pending.popleft().result())

    def _abandon(self) -> None:
        self._closed = True
        for future in self._pending:
            future.cancel()
        self._pending.clear()
        self._pool.shutdown(wait=True, cancel_futures=True)
