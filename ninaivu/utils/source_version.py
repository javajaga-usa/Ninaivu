"""Identify a local source version without reading a large video in full."""
import json
import hashlib
from pathlib import Path

#: Bytes sampled from each end of a file. A photograph smaller than twice this
#: is covered in full.
SAMPLE = 65536


def sample(path):
    """The file's size, and a digest of its first and last :data:`SAMPLE` bytes.

    The part of :func:`source_version` that is about the *contents*. The rest —
    path, device, file id, timestamps — describes where the file is and when
    it was touched, all of which change when a library is copied to another
    disk or machine even though not a byte of it has.
    """
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        digest.update(handle.read(SAMPLE))
        if size > SAMPLE:
            handle.seek(max(SAMPLE, size - SAMPLE))
            digest.update(handle.read(SAMPLE))
    return size, digest.hexdigest()


def source_version(path):
    path = Path(path).resolve()
    stat = path.stat()
    # Coarse filesystem timestamps can make immediate same-size rewrites look
    # identical. Sample both ends as well; small photos are covered in full.
    _, digest = sample(path)
    return json.dumps([str(path), stat.st_dev, stat.st_ino, stat.st_size,
                       stat.st_mtime_ns, stat.st_ctime_ns, digest])


def same_content(version, path):
    """Does *path* still hold the bytes *version* was taken of?

    Judged by size and the sampled digest only, so a file copied to another
    disk — new path, new file id, new timestamps — still counts as the same.
    ``None`` when it cannot be told: the file cannot be read, or *version* is
    missing or from an older format.
    """
    try:
        recorded = json.loads(version) if version else None
        size, digest = int(recorded[3]), str(recorded[6])
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    try:
        return sample(path) == (size, digest)
    except OSError:
        return None


def matches_source(cache, source):
    try:
        return Path(str(cache) + '.source').read_text(encoding='utf-8') == source_version(source)
    except OSError:
        return False


def stamp_source(cache, version):
    Path(str(cache) + '.source').write_text(version, encoding='utf-8')
