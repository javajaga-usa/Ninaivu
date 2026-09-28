"""Bounded, incremental log reading for the desktop panel."""
import codecs
import os
from pathlib import Path
import re


class LogTail:
    """Follow appended UTF-8 text without loading a whole server log."""

    def __init__(self, path, limit=65536):
        self.path = Path(path)
        self.limit = limit
        self.position = 0
        self.identity = None
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    def read(self):
        try:
            with self.path.open('rb') as stream:
                # Inspect the handle we opened: a rotated path may already
                # refer to another file by the time the read starts.
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                reset = self.identity != identity or stat.st_size < self.position
                skipped = stat.st_size - self.position > self.limit
                if reset or skipped:
                    self.position = max(0, stat.st_size - self.limit)
                    self.decoder.reset()
                self.identity = identity
                stream.seek(self.position)
                data = stream.read(self.limit)
                self.position = stream.tell()
                result = self.decoder.decode(data)
                if (reset or skipped) and stat.st_size > self.limit:
                    _, separator, complete_lines = result.partition('\n')
                    result = '[Showing the most recent log output]\n' + (complete_lines if separator else result)
                return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', result).replace('\r\n', '\n')
        except FileNotFoundError:
            self.identity = None
            self.position = 0
            self.decoder.reset()
            return ''
