"""
The consolidation engine.

Safety invariants, enforced here rather than documented and hoped for:

  I-1  Sources are only ever opened for reading. Nothing is written, renamed or
       deleted inside a source tree.
  I-2  Every file is SHA-256 hashed on the way out and re-hashed from disk after
       it lands. A copy is not 'done' until those two digests match.
  I-3  Nothing is deleted. Duplicates are recorded and left where they are. The
       only files this module removes are its own aborted .partial temporaries.
  I-4  A copy is atomic from the archive's point of view: bytes go to a
       .partial file and are renamed into place only after they are written, so
       a stop or a kill can never leave a truncated file at a real archive path.

Archive layout is YYYY/MM/DD, taken from the capture date.
"""

import errno
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import threading
import traceback
import time
from datetime import datetime
from pathlib import Path

from . import course_material, dates, entertainment, readahead, status_kit
from . import database as db
from .safety import (IS_WINDOWS, ARCHIVE_MARKER, MEDIA_KINDS, UNDATED_FOLDER,
                    check_free_space, is_within, long_path, normalise,
                    normalise_sources, resolve_destination, short_path,
                    validate_job, write_archive_marker)
from .pacing import ArchivePacer

try:
    import exifread
except ImportError:                                   # degraded, not fatal
    exifread = None

try:
    # Ninaivu always has Pillow, because the gallery needs it for thumbnails.
    # Standalone, a missing ExifRead meant every capture date silently fell
    # back to the filesystem timestamp and the whole archive piled into the
    # month it was built - the exact failure this tool exists to prevent, and
    # one you only notice by squinting at the year chart. Inside Ninaivu there
    # is a second reader available, so use it rather than lose the dates.
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None

CHUNK = 1024 * 1024          # 1 MiB - the old 8 KiB made large videos crawl

#: Files copied at once.
#:
#: **This is 1, and raising it is currently refused.** The machinery for
#: several workers is written and works — but two of the engine's guarantees do
#: not survive it yet, and both were caught by the test suite rather than
#: reasoned about:
#:
#: * two byte-identical photos can both be archived, because neither has
#:   verified at the moment the other checks for a duplicate;
#: * which of two same-named photos gets the plain name and which gets ``_1``
#:   depends on which copy finishes first, so a dry run stops predicting what
#:   the real run will do.
#:
#: Neither loses a photograph, but "the same bytes are stored once" and "a dry
#: run predicts the real run" are the reasons to trust this tool, and a
#: consolidation of a family's only copies is the wrong place to trade them for
#: throughput. Making it safe means deciding every destination name on the walk
#: thread, in walk order, and letting the workers do only the copying and
#: verifying — a change worth making properly rather than late at night.
DEFAULT_WORKERS = 1

#: Smallest file that counts as real media. Old drives are full of things that
#: are technically images - icons, sprites, email signatures, other software's
#: leftover thumbnails - and archiving them fills the master with noise that
#: has to be sifted out by hand later. Ninaivu overrides this from its config.
MIN_MEDIA_BYTES = 60 * 1024
PARTIAL_PREFIX = '.pam-partial-'
# UNDATED_FOLDER lives in safety.py because the archive-root detection there has
# to recognise it as part of the layout; re-exported here for callers.

# Deliberately exhaustive. A format missing from these sets meant the file was
# passed over in silence, which is the worst possible failure for a tool whose
# job is "lose nothing" - so the lists are long, and anything they still miss is
# caught by content sniffing (see sniff_media below).
IMAGE_EXTS = {
    # everyday
    'jpg', 'jpeg', 'jpe', 'jfif', 'jif', 'png', 'gif', 'bmp', 'dib', 'webp',
    'tif', 'tiff', 'ico', 'cur', 'svg', 'svgz', 'psd', 'psb', 'xcf', 'avif',
    'heic', 'heif', 'hif', 'jxl', 'jp2', 'j2k', 'jpf', 'jpx', 'jpm', 'tga',
    'pcx', 'ppm', 'pgm', 'pbm', 'pnm', 'exr', 'hdr', 'dds', 'wbmp', 'apng',
    'pict', 'pct', 'ai', 'eps', 'emf', 'wmf', 'djvu', 'djv', 'nrw',
    # camera raw, by vendor family
    'raw', 'arw', 'srf', 'sr2',                # Sony
    'cr2', 'cr3', 'crw',                       # Canon
    'nef',                                     # Nikon
    'orf',                                     # Olympus
    'raf',                                     # Fujifilm
    'rw2', 'rwz',                              # Panasonic
    'pef', 'ptx',                              # Pentax
    'dng',                                     # Adobe / universal
    'x3f',                                     # Sigma
    'erf',                                     # Epson
    'mef', 'mos',                              # Mamiya / Leaf
    'iiq', 'cap',                              # Phase One
    'k25', 'kdc', 'dcr', 'drf',                # Kodak
    'srw',                                     # Samsung
    'rwl',                                     # Leica
    'fff', '3fr',                              # Hasselblad
    'gpr',                                     # GoPro
    'braw', 'r3d',                             # Blackmagic / RED
}
VIDEO_EXTS = {
    'mp4', 'm4v', 'mov', 'qt', 'avi', 'mkv', 'webm', 'wmv', 'asf', 'flv', 'f4v',
    'swf', '3gp', '3g2', 'mpg', 'mpeg', 'mpe', 'm1v', 'm2v', 'mpv', 'ts', 'm2ts',
    'mts', 'tod', 'mod', 'vob', 'ogv', 'ogm', 'rm', 'rmvb', 'divx', 'xvid',
    'dv', 'dvr-ms', 'wtv', 'mxf', 'roq', 'nsv', 'amv', 'mtv', 'insv',
    'lrv', 'avchd', 'm4p', 'viv',
}
AUDIO_EXTS = {
    'mp3', 'wav', 'wave', 'flac', 'aac', 'ogg', 'oga', 'opus', 'wma', 'm4a',
    'm4b', 'm4r', 'aiff', 'aif', 'aifc', 'alac', 'amr', 'awb', 'ape', 'wv',
    'mpc', 'tta', 'ac3', 'dts', 'au', 'snd', 'ra', 'mid', 'midi', 'kar', 'caf',
    'gsm', 'dss', 'msv', 'dvf', 'voc', '8svx', 'sln', '3ga', 'aa', 'aax', 'mka',
}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

# Carried alongside their parent so face tags, edits and Takeout metadata are
# not stranded on the source drive.
SIDECAR_EXTS = {'xmp', 'aae', 'thm', 'json'}

# Directories that are never user media and that generate noise or recursion.
SKIP_DIRS = {'$recycle.bin', 'system volume information', '.trashes', '.spotlight-v100',
             '.fseventsd', '@eadir', '.thumbnails', '__pycache__', '.git', '.ninaivu_staging'}

# Extensions that are never a photo, a video or a sound recording.
#
# This list is consulted ONLY in fast-scan mode. Deep scan (the default) reads
# the first bytes of every unrecognised file regardless, so a JPEG someone
# renamed to .txt is still found. Fast scan trades that away: pointing the tool
# at a drive root means walking past hundreds of thousands of system files, and
# opening every one of them to read 32 bytes can dominate the run on a slow
# external disk.
#
# Anything genuinely ambiguous is deliberately NOT here - .dat, .bin, .tmp and
# files with no extension at all are sniffed in both modes, because those are
# exactly what old camcorders and phone dumps produce.
NEVER_MEDIA_EXTS = {
    # executables and system files
    'exe', 'dll', 'sys', 'msi', 'cab', 'bat', 'cmd', 'ps1', 'sh', 'com', 'scr',
    'drv', 'ocx', 'cpl', 'efi', 'mui', 'winmd', 'lnk', 'url', 'inf', 'reg',
    # code and text
    'txt', 'log', 'ini', 'cfg', 'conf', 'xml', 'yaml', 'yml', 'csv', 'tsv',
    'md', 'rst', 'html', 'htm', 'xhtml', 'css', 'scss', 'less', 'js', 'mjs',
    'ts', 'tsx', 'jsx', 'py', 'pyc', 'pyo', 'java', 'class', 'jar', 'c', 'cpp',
    'cc', 'h', 'hpp', 'cs', 'go', 'rs', 'php', 'rb', 'pl', 'lua', 'sql', 'patch',
    # archives and images of disks
    'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'zst', 'iso', 'dmg', 'pkg',
    'deb', 'rpm', 'apk', 'whl', 'egg',
    # documents
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'xlsm', 'ppt', 'pptx', 'odt', 'ods',
    'odp', 'rtf', 'epub', 'mobi', 'azw', 'azw3', 'pages', 'numbers', 'key',
    # data and mail
    'db', 'sqlite', 'sqlite3', 'mdb', 'accdb', 'pst', 'ost', 'eml', 'msg',
    'vcf', 'ics', 'edb', 'ldf', 'mdf',
    # fonts and build leftovers
    'ttf', 'otf', 'woff', 'woff2', 'eot', 'fon', 'pdb', 'obj', 'lib', 'so',
    'dylib', 'o', 'a', 'map',
}

# A capture date outside this window is a dead camera clock, not a real date.
# The floors live with the rest of the date evidence in dates.py, which the
# library index shares so both file a photograph under the same day.
MIN_YEAR = dates.MIN_YEAR
MIN_FS_YEAR = dates.MIN_FS_YEAR


def _claim_key(path: str) -> str:
    """How a reserved archive name is remembered.

    Case-folded, whatever the server runs: the archive is often an NTFS, exFAT
    or APFS disk, where IMG_1.JPG and img_1.jpg are one file. Compared exactly,
    two workers could both reserve "their" name, and the second rename replaced
    the first photograph. On a disk that tells them apart the cost is a ``_1``
    on one of two such names copied at the same moment.
    """
    return os.path.normpath(path).casefold()


class Cancelled(Exception):
    """Raised inside the worker when Stop is pressed."""


class DriveDisconnected(Exception):
    """Required source/destination storage disappeared during a run."""

    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__('; '.join(self.missing))


#: Errors that mean *the device went away*, not that this folder is unreadable.
#: A USB enclosure that resets mid-walk fails whichever folder was being listed
#: at that instant and is back a second later, so by the time the failure is
#: looked at the drive letter answers again and nothing seems wrong — except
#: that the folder has been written off and everything inside it left behind.
_DEVICE_WINERRORS = frozenset({
    21,      # ERROR_NOT_READY — "The device is not ready"
    433,     # ERROR_DEV_NOT_EXIST — "A device which does not exist was specified"
    1117,    # ERROR_IO_DEVICE — "because of an I/O device error"
    1167,    # ERROR_DEVICE_NOT_CONNECTED
})
_DEVICE_ERRNOS = frozenset({errno.EIO, errno.ENODEV, errno.ENXIO})

#: How many times one run will wait for the drive and walk again before it
#: stops believing the drive and starts recording folders as unreadable. A
#: cable or an enclosure on its way out can reset every few seconds, and a run
#: that restarts for every one of them never reaches the end.
WALK_RESETS_TOLERATED = 3


def looks_like_the_device_went_away(exc):
    """Whether this failure is the drive blinking rather than a bad folder."""
    winerror = getattr(exc, 'winerror', None)
    if winerror is not None:
        return winerror in _DEVICE_WINERRORS
    return getattr(exc, 'errno', None) in _DEVICE_ERRNOS


def _within_any(folder, roots, normalised=False):
    """Whether *folder* is one of *roots* (normalised) or lies inside one."""
    if not roots:
        return False
    current = folder if normalised else normalise(folder)
    while True:
        if current in roots:
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _measure_folder(folder, checkpoint=None, limit=200000):
    """(files, bytes) under *folder*, for showing what holding it back saves."""
    files = size = 0
    for root, _dirs, names in os.walk(long_path(folder)):
        if checkpoint:
            checkpoint()
        for name in names:
            if checkpoint:
                checkpoint()
            if files >= limit:
                return files, size, True
            files += 1
            try:
                size += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return files, size, False


def _volume_identity(path):
    """Stable storage identity, including Windows removable-drive serials."""
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes

            volume = ctypes.create_unicode_buffer(1024)
            if not ctypes.windll.kernel32.GetVolumePathNameW(
                    str(path), volume, len(volume)):
                return None
            serial = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetVolumeInformationW(
                    volume.value, None, 0, ctypes.byref(serial), None, None, None, 0):
                return None
            return ('windows-volume', int(serial.value))
        except Exception:  # noqa: BLE001 - fallback below
            pass
    try:
        return ('device', int(os.stat(long_path(path)).st_dev))
    except OSError:
        return None


class Gate:
    """
    Pause/stop that takes effect in milliseconds even in the middle of a 4 GB
    video, because workers check it between chunks rather than between files.

    The fast path (running, not cancelled) is two Event.is_set() calls, so it is
    cheap enough to sit inside the copy loop. Pausing parks on an Event rather
    than spinning on time.sleep().
    """

    def __init__(self):
        self._resume = threading.Event()
        self._resume.set()
        self._cancel = threading.Event()
        self.pacer = None

    def pause(self):
        self._resume.clear()

    def resume(self):
        self._resume.set()

    def cancel(self):
        self._cancel.set()
        self._resume.set()          # unpark anyone parked on the pause gate

    @property
    def is_paused(self):
        return not self._resume.is_set()

    @property
    def is_cancelled(self):
        return self._cancel.is_set()

    def check(self):
        if self._cancel.is_set():
            raise Cancelled()
        if not self._resume.is_set():
            while not self._resume.wait(0.25):
                if self._cancel.is_set():
                    raise Cancelled()
            if self._cancel.is_set():
                raise Cancelled()


# --------------------------------------------------------------------------
# file classification
# --------------------------------------------------------------------------

def _ext(filename):
    return os.path.splitext(filename)[1].lstrip('.').lower()


def is_supported_media(filename):
    return _ext(filename) in MEDIA_EXTS


def is_sidecar(filename):
    return _ext(filename) in SIDECAR_EXTS


#: Extensions two worlds claim. ``.ts`` is an MPEG transport stream and also
#: TypeScript; ``.mts`` is a camcorder's AVCHD clip and also a TypeScript
#: module. Trusting the extension archived ``typescript.d.ts`` and friends as
#: videos, so for these the first bytes decide (see looks_like_transport_stream).
AMBIGUOUS_EXTS = {'ts', 'mts'}

_TS_SYNC = 0x47


def looks_like_transport_stream(head):
    """True when *head* (the first 400 bytes) is an MPEG transport stream.

    Every packet starts with the sync byte 0x47. A broadcast or downloaded
    ``.ts`` has 188-byte packets from the first byte; a camcorder's AVCHD
    ``.mts``/``.m2ts`` (and a Blu-ray's) puts a 4-byte timestamp before each,
    making 192-byte packets whose sync byte is at offset 4. Three packets in a
    row tell a stream from a text file that merely opens with a ``G``.
    """
    plain = len(head) >= 377 and all(head[i] == _TS_SYNC for i in (0, 188, 376))
    stamped = len(head) >= 389 and all(head[i] == _TS_SYNC for i in (4, 196, 388))
    return plain or stamped


def classify_by_extension(filename):
    """Return 'image' | 'video' | 'audio' | None.

    An ambiguous extension (:data:`AMBIGUOUS_EXTS`) still answers 'video' here;
    the archive walk confirms it from the file's bytes before taking it.
    """
    ext = _ext(filename)
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in AUDIO_EXTS:
        return 'audio'
    return None


# Magic numbers, checked against the first bytes of a file. This is the safety
# net behind the extension lists: a photo named IMG_0042 with no extension at
# all, a video saved as .dat by an old camcorder, or a JPEG someone renamed to
# .txt would all otherwise be skipped without ever being mentioned.
def sniff_media(filepath):
    """Identify media by content. Returns 'image' | 'video' | 'audio' | None."""
    try:
        with open(long_path(filepath), 'rb') as f:
            stream_head = f.read(400)
    except OSError:
        return None
    head = stream_head[:32]
    if len(head) < 4:
        return None

    # --- container formats keyed off a box/chunk type ---
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand[:3] in (b'hei', b'avi', b'mif', b'msf', b'cr3'):
            return 'image'                    # HEIC/HEIF, AVIF, Canon CR3
        if brand[:3] in (b'M4A', b'M4B', b'M4P'):
            return 'audio'
        return 'video'                        # mp4, mov, 3gp, and friends
    if head[:4] == b'RIFF':
        kind = head[8:12]
        if kind == b'WEBP':
            return 'image'
        if kind == b'AVI ':
            return 'video'
        if kind == b'WAVE':
            return 'audio'
    if head[:4] == b'FORM' and head[8:12] in (b'AIFF', b'AIFC', b'8SVX'):
        return 'audio'

    # --- images ---
    if head[:3] == b'\xff\xd8\xff':                       return 'image'   # JPEG
    if head[:8] == b'\x89PNG\r\n\x1a\n':                  return 'image'
    if head[:6] in (b'GIF87a', b'GIF89a'):                return 'image'
    if head[:4] in (b'II*\x00', b'MM\x00*'):              return 'image'   # TIFF + most RAW
    if head[:4] in (b'IIRO', b'IIU\x00', b'II\x1a\x00'):  return 'image'   # ORF, RW2, RAF-ish
    if head[:4] == b'\x00\x00\x00\x0cjP  ':               return 'image'   # JPEG 2000
    if head[:2] == b'\xff\x0a' or head[:4] == b'\x00\x00\x00\x0cJXL ':
        return 'image'                                                     # JPEG XL
    if head[:4] == b'8BPS':                               return 'image'   # PSD
    if head[:4] == b'\x00\x00\x01\x00':                   return 'image'   # ICO
    if head[:4] == b'\x00\x00\x02\x00':                   return 'image'   # CUR
    if head[:4] == b'FOVb':                               return 'image'   # Sigma X3F
    if head[:15] == b'FUJIFILMCCD-RAW':                   return 'image'   # Fujifilm RAF
    if head[:4] == b'\x76\x2f\x31\x01':                   return 'image'   # OpenEXR
    if head[:2] == b'BM' and len(head) >= 6:              return 'image'   # BMP
    if head[:2] in (b'P1', b'P2', b'P3', b'P4', b'P5', b'P6') and head[2:3].isspace():
        return 'image'                                                     # netpbm
    if head[:5] == b'<?xml' and b'svg' in head[:32].lower():
        return 'image'
    if head[:4] == b'<svg':                               return 'image'

    # --- video ---
    if head[:4] == b'\x1aE\xdf\xa3':                      return 'video'   # Matroska/WebM
    if head[:4] == b'\x30\x26\xb2\x75':                   return 'video'   # ASF/WMV
    if head[:3] == b'FLV':                                return 'video'
    if head[:4] in (b'\x00\x00\x01\xba', b'\x00\x00\x01\xb3'):
        return 'video'                                                     # MPEG PS/ES
    if looks_like_transport_stream(stream_head):
        return 'video'                                                     # MPEG-TS / AVCHD
    if head[:4] == b'.RMF':                               return 'video'   # RealMedia

    # --- audio ---
    if head[:4] == b'fLaC':                               return 'audio'
    if head[:4] == b'OggS':                               return 'audio'   # also ogv, rare
    if head[:3] == b'ID3':                                return 'audio'   # MP3 w/ tag
    if head[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2', b'\xff\xfa'):
        return 'audio'                                                     # bare MP3 frame
    if head[:4] in (b'\xff\xf1', b'\xff\xf9'):            return 'audio'   # ADTS AAC
    if head[:4] == b'MThd':                               return 'audio'   # MIDI
    if head[:4] == b'.snd':                               return 'audio'   # AU
    if head[:4] == b'MAC ':                               return 'audio'   # Monkey's Audio
    if head[:4] == b'wvpk':                               return 'audio'   # WavPack
    if head[:4] == b'TTA1':                               return 'audio'
    if head[:5] == b'#!AMR':                              return 'audio'
    if head[:4] == b'caff':                               return 'audio'   # CAF
    return None


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------

def hash_file(filepath, gate=None):
    """SHA-256 in 1 MiB chunks, interruptible between chunks."""
    h = hashlib.sha256()
    pacer = getattr(gate, 'pacer', None) if gate is not None else None
    last = 0
    with open(long_path(filepath), 'rb') as f:
        while True:
            if gate is not None:
                gate.check()
            if pacer is not None:
                # Charged for the bytes the last read got, not per read: the
                # read that finds the end of the file costs nothing.
                pacer.checkpoint(gate, last)
            chunk = f.read(CHUNK)
            if not chunk:
                break
            last = len(chunk)
            h.update(chunk)
    return h.hexdigest()


# Backwards-compatible alias for anything that imported the old name.
get_file_hash = hash_file


# --------------------------------------------------------------------------
# capture date
# --------------------------------------------------------------------------

_EXIF_RE = re.compile(r'^(\d{4})[:\-](\d{2})[:\-](\d{2})')


def _parse_exif_datetime(raw):
    """
    Turn an EXIF datetime string into a datetime, rejecting the junk that the
    old positional slicing waved through.

    '0000:00:00 00:00:00' is extremely common on flatbed scanners and on phones
    with a flat battery. It passes an isdigit() check and used to produce a
    literal 0000/00/00 folder in the archive.
    """
    if not raw:
        return None
    m = _EXIF_RE.match(str(raw).strip())
    if not m:
        return None
    year, month, day = (int(x) for x in m.groups())
    try:
        dt = datetime(year, month, day)
    except ValueError:               # e.g. 31 February, or month 00
        return None
    # Before 1900, or after tomorrow: a camera whose clock was never set.
    return dt if dates.plausible(dt, MIN_YEAR) else None


#: EXIF tag ids for the same three timestamps ExifRead is asked for, in the
#: same order of preference: taken, digitised, then last modified in-camera.
_PIL_DATE_TAGS = (36867, 36868, 306)


_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def _png_exif(filepath):
    """The payload of a PNG's eXIf chunk, or None, without decoding a pixel.

    A PNG is a signature and then chunks, each announcing its own length, so
    the one chunk wanted is reached by stepping over the others.
    """
    try:
        with open(long_path(filepath), 'rb') as f:
            if f.read(8) != _PNG_SIGNATURE:
                return None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                length, kind = struct.unpack('>I4s', head)
                if kind == b'eXIf':
                    return f.read(length)
                if kind == b'IEND':
                    return None
                f.seek(length + 4, os.SEEK_CUR)          # the data and its CRC
    except (OSError, struct.error):
        return None


def _exif_date(filepath):
    """The capture date from the file's own metadata, or None.

    Tries ExifRead first, which understands more makernotes and more broken
    files, then Pillow, which is always present under Ninaivu. Either reader
    finding a date is better than falling through to the filesystem.
    """
    if exifread is not None:
        try:
            with open(long_path(filepath), 'rb') as f:
                tags = exifread.process_file(f, details=False)
            for tag in ('EXIF DateTimeOriginal', 'EXIF DateTimeDigitized',
                        'Image DateTime'):
                if tag in tags:
                    dt = _parse_exif_datetime(tags[tag])
                    if dt:
                        return dt
        except Exception:
            pass

    if _PILImage is not None:
        try:
            import warnings
            with warnings.catch_warnings():
                # A file with damaged EXIF is ordinary here, not news: the
                # date chain has three more sources after this one.
                warnings.simplefilter("ignore")
                with _PILImage.open(long_path(filepath)) as img:
                    if img.format == 'PNG' and 'exif' not in img.info \
                            and 'Raw profile type exif' not in img.info:
                        # The PNG format lets the EXIF chunk sit after the
                        # pixel data, so when none was seen on the way in
                        # Pillow's getexif() decodes the whole picture to
                        # look for one there. Screenshots are the common PNG
                        # and carry none, so that was a full decode per file
                        # to find nothing. The chunk table is walked here
                        # instead: a few seeks, no pixels, the same answer.
                        data = _png_exif(filepath)
                        if not data:
                            return None
                        exif = _PILImage.Exif()
                        exif.load(data)
                    else:
                        exif = img.getexif()
                    raw = dict(exif or {})
                    # DateTimeOriginal and DateTimeDigitized live in the Exif
                    # sub-IFD, not the top-level one `getexif()` returns. Only
                    # tag 306 is up here, so reading just the top level meant
                    # that without ExifRead installed — which requirements.txt
                    # calls optional — Ninaivu found no capture date on an
                    # ordinary camera JPEG and filed it under today instead of
                    # the day it was taken.
                    try:
                        raw.update(exif.get_ifd(0x8769) or {})
                    except Exception:                    # noqa: BLE001
                        pass
            for tag in _PIL_DATE_TAGS:
                value = raw.get(tag)
                if value:
                    dt = _parse_exif_datetime(value)
                    if dt:
                        return dt
        except Exception:
            # Not an image, or an image Pillow cannot open: the next date
            # source down the chain will deal with it.
            pass
    return None


def capture_date(filepath, st=None, kind=None):
    """Return (datetime | None, source_label).

    EXIF first; then, in dates.fallback_date, the recording date inside a video,
    a Google Takeout sidecar, a date in the filename, a dated folder weighed
    against the file's timestamp, and the timestamp itself. Labels: ``exif``,
    ``container``, ``takeout-json``, ``filename``, ``folder``, ``filesystem``,
    ``none``.

    File timestamps rank last for a reason worth keeping in mind: exFAT stores
    local time and NTFS stores UTC, so they shift when a photo crosses
    filesystems, and every copy through an old backup can push them later.

    ``st`` is the file's stat result when the caller already has it, so the
    timestamp is not fetched a second time. ``kind`` is 'image', 'video' or
    'audio' when known; otherwise the extension decides. Neither EXIF reader
    understands a video or audio container, but both would still open the
    file and search it for a header that is not there, so for those the chain
    starts at the recording date inside the container.
    """
    if kind is None:
        kind = classify_by_extension(filepath)
    if kind in ('video', 'audio'):
        return dates.fallback_date(filepath, st=st)
    dt = _exif_date(filepath)
    if dt:
        return dt, 'exif'
    return dates.fallback_date(filepath, st=st)


def target_folder(destination, dt, source_path=None):
    """YYYY/MM/DD, or Unknown-Date when no plausible date could be established.

    Undated files go one level further down, into a folder named after the
    one they came from: ``Unknown-Date/WhatsApp Images/``. Every undated file
    of every import used to share one flat folder, which old drives full of
    ``image.jpg`` can fill to the ``_9999`` limit on a name, and which no file
    manager lists quickly. The source folder's name is also the best hint a
    person sorting them by hand will get. Files already archived stay where
    they are; a name that cannot be used, or that reads as a date, falls back
    to the flat folder.
    """
    if dt is None:
        undated = os.path.join(destination, UNDATED_FOLDER)
        if source_path:
            from ..utils.filenames import safe_filename      # noqa: PLC0415
            parent = os.path.basename(os.path.dirname(os.path.abspath(source_path)))
            name = safe_filename(parent)
            # A dated-looking name ("2015", "2017-07 Kerala") would make the
            # gallery date what the archive could not.
            if name and name != UNDATED_FOLDER \
                    and dates.folder_period(f'{name}/x') is None:
                return os.path.join(undated, name)
        return undated
    return os.path.join(destination, f'{dt.year:04d}', f'{dt.month:02d}', f'{dt.day:02d}')


# --------------------------------------------------------------------------
# Windows creation time
# --------------------------------------------------------------------------

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


def source_is_hidden(filepath, folder_cache=None):
    """Was this file, or any folder above it, deliberately put out of sight?

    Checked so the archive does not launder a hidden photograph into a plainly
    named YYYY/MM/DD folder. Dot-prefixed names count everywhere; the Windows
    hidden and system attributes count on Windows.

    ``folder_cache`` remembers the answer for each folder. Every photo used to
    ask Windows about every folder above it, so a card of 5,000 photos in one
    folder six levels deep made 35,000 attribute calls to learn one fact six
    times over. The source is read-only for the length of a run -- the same
    assumption the sidecar cache makes -- so a run passes one dict and each
    folder is asked about once. Without it the answer is identical, only slower.
    """
    parts = os.path.abspath(filepath).replace('\\', '/').split('/')
    if '.ninaivu_staging' in parts:
        # Copied off a phone into Ninaivu's own staging folder, whose dot says
        # nothing about whether anybody hid the photograph. Only the phone's
        # own folders, below the per-device one, are theirs. Judged on the
        # whole path, every photograph imported from a phone was archived
        # hidden.
        below = parts[parts.index('.ninaivu_staging') + 2:]
        return any(part.startswith('.') for part in below)
    for part in parts[1:]:
        if part.startswith('.'):
            return True
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        get_attributes = ctypes.windll.kernel32.GetFileAttributesW
        concealing = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM

        def marked(path):
            attributes = get_attributes(str(path))
            return attributes != -1 and bool(attributes & concealing)

        def at_drive_root(path):
            return os.path.splitdrive(path)[1] in ('\\', '/', '')

        def folder_hidden(folder):
            if at_drive_root(folder):
                return False            # a drive root is never "hidden" here
            if folder_cache is not None and folder in folder_cache:
                return folder_cache[folder]
            parent = os.path.dirname(folder)
            hidden = marked(folder) or (parent != folder and folder_hidden(parent))
            if folder_cache is not None:
                folder_cache[folder] = hidden
            return hidden

        current = os.path.abspath(filepath)
        if at_drive_root(current):
            return False
        if marked(current):
            return True
        parent = os.path.dirname(current)
        return parent != current and folder_hidden(parent)
    except Exception:                             # noqa: BLE001 - best effort
        return False


def mark_hidden(filepath):
    """Carry a source's hidden-ness onto the archived copy.

    Only Windows has an attribute for this. On other platforms the alternative
    would be renaming the file to a dot name, which changes what the archive
    holds - so the archive records it and the library's own rules apply.
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.SetFileAttributesW(
            str(filepath), FILE_ATTRIBUTE_HIDDEN))
    except Exception:                             # noqa: BLE001
        return False


def set_windows_ctime(filepath, ctime):
    """Best-effort restore of the NTFS creation timestamp. No-op off Windows."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes, byref, windll

        class FILETIME(ctypes.Structure):
            _fields_ = [('dwLowDateTime', wintypes.DWORD),
                        ('dwHighDateTime', wintypes.DWORD)]

        stamp = int((ctime + 11644473600) * 10_000_000)
        ft = FILETIME(stamp & 0xFFFFFFFF, stamp >> 32)
        GENERIC_WRITE, OPEN_EXISTING, FLAG_BACKUP_SEMANTICS = 0x40000000, 3, 0x02000000
        handle = windll.kernel32.CreateFileW(
            long_path(filepath), GENERIC_WRITE, 0, None,
            OPEN_EXISTING, FLAG_BACKUP_SEMANTICS, None)
        if handle not in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            windll.kernel32.SetFileTime(handle, byref(ft), None, None)
            windll.kernel32.CloseHandle(handle)
    except Exception:
        pass



# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------

MODE_COPY = 'copy'
MODE_DRY_RUN = 'dry-run'
MODE_VERIFY = 'verify'

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')


class ArchiveJob:
    """
    One consolidation run.

    Three modes share the same walk and the same date/dedup logic, so a dry run
    predicts exactly what a real run will do rather than approximating it:

      copy     - the real thing
      dry-run  - decides everything, writes nothing. Rows land in plan-* states.
      verify   - re-reads every archived file and re-checks it against the
                 SHA-256 recorded when it was written. This is the audit that
                 lets someone actually trust the archive before wiping a drive.
    """

    def __init__(self, sources, destination, mode=MODE_COPY, media_types=None,
                 workers=None,
                 deep_scan=True, pacer=None, vision=None):
        # Each source carries its own media selection, so one folder can be
        # scanned for photos only while another is scanned for video only.
        # `media_types` is the fallback for sources given as bare paths.
        self.source_entries = normalise_sources(sources, media_types)
        self.sources = [e['path'] for e in self.source_entries]
        self.destination = os.path.abspath(os.path.expanduser(destination))
        self.mode = mode
        # Not `media_types or MEDIA_KINDS`: an explicitly empty selection is a
        # request to archive nothing, and `or` would turn it into everything.
        # Enforcement is per-source via `_wanted`; this is the job-wide record.
        self.media_types = (frozenset(MEDIA_KINDS) if media_types is None
                            else frozenset(media_types) & frozenset(MEDIA_KINDS))
        #: How many files to copy at once.
        #:
        #: The work per file is read, hash, write, fsync, re-read, re-hash —
        #: a mix of waiting on a disk and burning a core, and one thread can
        #: only ever do one of them at a time. Overlapping them is where the
        #: speed is.
        #:
        #: More is not always better: on a single spinning drive, concurrent
        #: reads turn a sequential scan into a seek storm and can be *slower*.
        #: The default is deliberately modest, and `workers=1` restores exactly
        #: the old sequential behaviour.
        self.workers = max(1, int(workers or DEFAULT_WORKERS))
        if self.workers > 1:
            # Refuse rather than clamp. A silently ignored setting is how
            # somebody ends up believing they turned this on.
            raise ValueError(
                'Copying with more than one worker is not safe yet: identical '
                'photos can be archived twice and a dry run stops predicting '
                'the real run. See DEFAULT_WORKERS in this module.')
        # Deep scan reads the first bytes of every unrecognised file, so a photo
        # with the wrong extension is still found. Fast scan trusts the
        # extension for known non-media types - much quicker over a whole drive,
        # at the cost of missing a misnamed file.
        self.deep_scan = bool(deep_scan)
        self.gate = Gate()
        self.pacer = pacer or ArchivePacer()
        self.gate.pacer = self.pacer
        self.job_id = None
        self.thread = None
        self.total_files = 0
        self.total_bytes = 0
        self.processed = 0
        #: Of `processed`, the files an earlier run had already finished. A
        #: resumed walk meets them mixed in with new work, and counting them as
        #: progress made a correct resume look like the archive being redone.
        self.stepped_over = 0
        self.sidecars_copied = 0
        self.sidecars_failed = 0
        self.counter = 0
        self.started_at = time.time()
        # How many files this archive had already settled before this run
        # touched anything. Start has always been Resume; this is what lets it
        # say so instead of leaving somebody to infer it from a fast counter.
        self.resumed_from = 0
        #: Of `total_files`, how many were already finished when the count was
        #: taken. `resumed_from` is the whole archive and can exceed this run's
        #: own total; None when the count came from an estimate and was not taken.
        self.finished_in_run = None
        self.non_media = 0           # files that are genuinely not media
        self.sniffed = 0              # media rescued by content sniffing
        self.filtered_out = 0         # media excluded by this source's selection
        self.filtered_by_kind = {}
        self.included_by_kind = {}    # media taken, by kind
        self.too_small = 0            # media under MIN_MEDIA_BYTES
        self.hidden_carried = 0       # copies marked hidden like their source
        self.last_kind = None         # kind of the file _walk() yielded last
        self.skipped_system_dirs = 0  # $RECYCLE.BIN and friends
        #: Folders that look like course material, as found by the last walk:
        #: dicts of path, reasons, score, state and (when measured) files and
        #: bytes. state is 'held' (not copied until someone decides),
        #: 'excluded', 'kept' (someone said keep) or 'review' (kept because it
        #: holds camera photographs, but still shown to be looked at).
        self.course_folders = []
        #: Set by the estimate, which shows sizes; a run does not need them.
        self.measure_course_folders = False
        self.course_progress = None
        #: The image model, when one is loaded: a second opinion for folders the
        #: names leave one step short of the threshold. None runs without it.
        self.vision = vision
        self._course_verdicts = {}    # folder -> verdict, stable across both walks
        self._course_logged = set()
        #: Films, TV and music found by the last walk, one entry per folder and
        #: category: dicts of path, category, state, reasons, files, bytes,
        #: examples, and the files spared for looking personal. state is 'held',
        #: 'excluded', 'kept', or 'review' (nothing held; only files that looked
        #: like entertainment but seem personal, all copied).
        self.entertainment = []
        self.set_aside_files = 0      # films and songs not taken by the last walk
        self._entertainment_verdicts = {}   # folder -> (entries, names to skip)
        self._entertainment_logged = set()
        self.pruned_destination = 0   # folders skipped for being the archive
        self.unreadable_dirs = []     # folders that could not be listed at all
        self._unreadable_seen = set()
        #: Phones and cameras not fully copied off: (source, what happened).
        self.device_shortfalls = []
        #: How many times this run has restarted because the drive reset. Kept
        #: on the job rather than on the walk, because the restart *is* a new
        #: walk and a per-walk count would never reach its limit.
        self.device_resets = 0
        self.deepest = 0              # deepest folder level reached
        self.resolution = {'destination': self.destination, 'corrected': False,
                           'original': self.destination, 'reason': None, 'message': ''}
        self._sniff_cache = {}
        #: Threads listing folders and reading file headers ahead of a counting
        #: walk. 0 reads everything on the walk's own thread.
        self.read_ahead_threads = readahead.DEFAULT_THREADS
        self._copying = False         # the copying walk reads nothing ahead
        self.last_size = 0            # size of the file _walk() yielded last
        self._sidecar_cache = {}   # folder -> its sidecar names
        self._companion_cache = {}  # folder -> {casefolded stem: media names}
        self._hidden_folders = {}  # folder -> hidden by attribute (source is read-only)
        # Copying runs on several threads; deciding does not. These guard the
        # three places where two workers could otherwise reach different
        # conclusions than one worker would have. See `workers` below.
        self._decide_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._last_heartbeat = time.time()
        self._inflight = {}        # sha256 -> Event, for files being copied now
        self._claimed = set()      # archive paths a worker has taken but not filled
        self._made_dirs = set()    # archive folders already created
        self._log = None
        self.waiting_for = []      # unavailable roots shown in live status
        self.reconnects = 0
        # Remember the actual volume behind each required path. On Unix an
        # unplugged mount can reveal an ordinary empty directory underneath;
        # checking existence alone would then write the archive to the system
        # disk. A changed st_dev is treated as unavailable until the same
        # volume returns.
        self._drive_probes = []
        for source in self.sources:
            self._remember_drive('source', source)
        self._remember_drive('destination', self.destination)

    # ---------------- logging ----------------

    def _open_log(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S')
            path = os.path.join(LOG_DIR, f'run-{stamp}-{self.mode}.log')
            self._log = open(path, 'a', encoding='utf-8', buffering=1)
            self.log(f'mode={self.mode} destination={self.destination} '
                     f'scan={"deep" if self.deep_scan else "fast"}')
            for entry in self.source_entries:
                kinds = ','.join(k for k in MEDIA_KINDS if k in entry['types']) or 'none'
                self.log(f'source={entry["path"]} types={kinds}')
        except OSError:
            self._log = None

    #: Seconds between the progress lines a long run leaves in its log.
    #: Silence is indistinguishable from a hang. The run that prompted this
    #: went twelve minutes without a line while it hashed eight gigabytes of
    #: drone video against the archive, and was stopped by hand for looking
    #: stuck — the log is the only place that could have said otherwise.
    HEARTBEAT_SECONDS = 60.0

    def _heartbeat(self):
        """Leave a progress line in the run log, about once a minute."""
        now = time.time()
        with self._count_lock:
            if now - self._last_heartbeat < self.HEARTBEAT_SECONDS:
                return
            self._last_heartbeat = now
            processed, stepped = self.processed, self.stepped_over
            total, since = self.total_files, self.started_at
        worked = max(0, processed - stepped)
        elapsed = max(0.001, now - since)
        self.log(f'PROGRESS {processed:,} of {total:,} files checked · '
                 f'{worked:,} new this run · {stepped:,} already done, stepped '
                 f'over · {worked / elapsed * 60:.1f} new files a minute')

    def log(self, message):
        """A plain-text record that outlives the database and the browser tab."""
        if self._log is None:
            return
        try:
            self._log.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}\n')
        except (OSError, ValueError):
            pass

    def _close_log(self):
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    # ---------------- removable-drive recovery ----------------

    @staticmethod
    def _existing_probe(path):
        """Nearest existing path, so a not-yet-created destination is tracked."""
        probe = os.path.abspath(path)
        while not os.path.exists(long_path(probe)):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        return probe

    def _remember_drive(self, role, path):
        probe = self._existing_probe(path)
        try:
            identity = _volume_identity(probe)
        except OSError:
            identity = None
        self._drive_probes.append({
            'role': role, 'path': os.path.abspath(path),
            'probe': probe, 'identity': identity,
        })

    def _unavailable_roots(self):
        missing = []
        for item in self._drive_probes:
            # A source must remain a directory. For a new destination, the
            # remembered existing parent is the availability probe until the
            # archive directory itself has been created.
            target = item['path'] if item['role'] == 'source' else item['probe']
            try:
                os.stat(long_path(target))
                available = os.path.isdir(long_path(target))
            except OSError:
                available = False
            if not available:
                missing.append(f"{item['role']} unavailable: {item['path']}")
            elif (item['identity'] is not None
                  and _volume_identity(target) != item['identity']):
                missing.append(f"{item['role']} has a different volume: {item['path']}")
        return missing

    def _promote_destination_probe(self):
        """Once created, require the archive directory itself to stay present."""
        for item in self._drive_probes:
            if item['role'] == 'destination':
                item['probe'] = item['path']

    def _raise_if_disconnected(self):
        missing = self._unavailable_roots()
        if missing:
            raise DriveDisconnected(missing)

    def _wait_for_drives(self, outage):
        """Wait interruptibly for the same storage to return."""
        last = None
        while True:
            self.gate.check()
            missing = self._unavailable_roots()
            if not missing:
                break
            self.waiting_for = missing
            if missing != last:
                message = ('Drive disconnected — waiting safely; reconnect the '
                           'same drive to resume. ' + '; '.join(missing))
                db.update_job(self.job_id, state='running',
                              phase='waiting-for-drive', message=message)
                self.log('WAITING FOR DRIVE: ' + '; '.join(missing))
                last = list(missing)
            time.sleep(0.5)
        self.waiting_for = []
        self.reconnects += 1
        self.log('DRIVE RESTORED: restarting safely from verified progress')
        db.update_job(self.job_id, state='recovered', phase='restarting',
                      ended_at=time.time(),
                      message='Drive restored — resuming from verified progress.')

    # ---------------- enumeration ----------------

    def _wanted(self, root, filename, allowed, entry=None, ahead=None):
        """
        Decide whether a file is media this source was asked to archive.

        `allowed` is the media selection for the source folder currently being
        walked, not a global setting - two folders in the same job can be
        scanned for different things. `entry` is the file's directory entry
        from the walk, which carries its size; `ahead` is the walk's read-ahead,
        which may already have read the file's first bytes.

        Extension first, because it is free. If the extension says nothing -
        no extension at all, or something unrecognised - fall back to reading
        the first bytes. That is what stops a photo saved as IMG_0042 with no
        suffix, or a camcorder file named CLIP.DAT, from being skipped in
        silence. Sniff results are cached because the tree is walked twice
        (once to count, once to copy) and the answer cannot change in between.
        """
        size = self._size(root, filename, entry)
        self.last_size = size or 0
        kind = classify_by_extension(filename)
        if kind is not None and _ext(filename) in AMBIGUOUS_EXTS:
            # The extension is a question here, not an answer: read the bytes.
            # Cached like a sniff, because both walks must agree - but not for
            # a file under the floor, which is skipped whatever it holds.
            if size is not None and size < MIN_MEDIA_BYTES:
                self.too_small += 1
                return False
            kind, _read_now = self._sniffed(root, filename, entry, ahead)
            if kind is None:
                self.non_media += 1
                return False
        if kind is None:
            if (is_sidecar(filename) or filename.startswith(PARTIAL_PREFIX)
                    or filename == ARCHIVE_MARKER):
                return False
            if not self.deep_scan and _ext(filename) in NEVER_MEDIA_EXTS:
                # Fast scan only: trust the extension for known non-media types
                # rather than opening every .dll on the system to read 32 bytes.
                self.non_media += 1
                return False
            if size is not None and size < MIN_MEDIA_BYTES:
                # Too small to be kept whatever it turns out to hold, so opening
                # it would learn nothing. On a real drive this is most of the
                # unrecognised files - class files, web pages, program data -
                # and reading each one cost a seek: 79% of the files a deep
                # scan opened on a measured 8-minute walk.
                self.too_small += 1
                return False
            kind, read_now = self._sniffed(root, filename, entry, ahead)
            if read_now and kind is not None:
                self.sniffed += 1
            if kind is None:
                self.non_media += 1
                return False
        if kind not in allowed:
            # Deliberately excluded, not overlooked. Counted so the run can say
            # how much it left behind rather than quietly narrowing the job.
            self.filtered_out += 1
            self.filtered_by_kind[kind] = self.filtered_by_kind.get(kind, 0) + 1
            return False
        # Below the floor it is not a photograph, whatever its extension says.
        # Checked after the kind is known so the count of "not media" stays
        # about formats, and this shows up as its own tally.
        if size is not None and size < MIN_MEDIA_BYTES:
            self.too_small += 1
            return False
        # What is being taken, by kind. An estimate that reads "56,945 files,
        # 1.4 TB" is unfalsifiable; one that reads "12,400 photos · 44,545
        # video" shows a misconfigured selection at a glance.
        self.included_by_kind[kind] = self.included_by_kind.get(kind, 0) + 1
        # The kind of the file just accepted, for a caller iterating _walk() who
        # needs to attribute the file's size without classifying it again.
        self.last_kind = kind
        return True

    def _sniffed(self, root, filename, entry=None, ahead=None):
        """(kind by content, read now): cached across both walks, read ahead if it was."""
        path = os.path.join(root, filename)
        if path in self._sniff_cache:
            return self._sniff_cache[path], False
        disk_path = entry.path if entry is not None else path
        kind = ahead.read('sniff', disk_path) if ahead is not None else sniff_media(disk_path)
        self._sniff_cache[path] = kind
        return kind, True

    @staticmethod
    def _size(root, filename, entry=None):
        """A file's size: from the walk's directory entry when there is one."""
        if entry is not None:
            return readahead.entry_size(entry)
        try:
            return os.path.getsize(long_path(os.path.join(root, filename)))
        except OSError:
            return None

    def _on_walk_error(self, exc):
        """
        A folder that could not be listed.

        os.walk's default is to swallow these completely: one PermissionError or
        one bad sector on a directory and the ENTIRE subtree below it is skipped
        without a word, while the run still reports success. On a whole-drive
        scan that can be thousands of photos. Recording it here is what turns a
        silent hole into a visible error the user can act on.
        """
        self._raise_if_disconnected()
        path = getattr(exc, 'filename', None) or '<unknown folder>'

        # A drive that dropped off the bus and came straight back is not an
        # unreadable folder, and treating it as one silently leaves everything
        # below it out of the archive — on the run this was found on, 61
        # folders of family photographs, in a run that still said "complete".
        # The drive is usually back by now, so waiting for it returns at once
        # and the walk starts again; anything already archived is stepped over.
        if (looks_like_the_device_went_away(exc)
                and self.device_resets < WALK_RESETS_TOLERATED):
            self.device_resets += 1
            self.log(f'DRIVE RESET while listing {path}: {exc} — waiting for '
                     f'the drive, then reading the source again '
                     f'({self.device_resets} of {WALK_RESETS_TOLERATED})')
            raise DriveDisconnected([f'the source drive reset while listing '
                                     f'{short_path(str(path))}'])

        if path not in self._unreadable_seen:
            self._unreadable_seen.add(path)
            self.unreadable_dirs.append((short_path(str(path)), str(exc)))
            self.log(f'UNREADABLE FOLDER {path}: {exc}')

    def _course_verdict(self, folder, subdirs, files, neighbours, volume):
        """Whether *folder* is course material, and what to do about it.

        Worked out once per folder and remembered, because a run walks the
        tree twice and must reach the same answer both times -- a folder held
        back while counting and copied while copying would make the estimate
        and the dry run lie about the real run.
        """
        if folder in self._course_verdicts:
            return self._course_verdicts[folder]
        verdict = None
        judged = course_material.assess(os.path.basename(folder), subdirs, files,
                                        neighbours)
        # Only one point short, so the pictures of an ordinary folder -- a trip's
        # numbered days score two -- are never put in front of the model.
        if judged.score == course_material.THRESHOLD - 1:
            points, detail = self._picture_opinion(folder, volume)
            if points:
                judged.add(points, detail)
        if judged.is_course:
            key = course_material.decision_key(folder, volume)
            decision = db.folder_decision(key)
            photos = 0
            if decision == 'include':
                state = 'kept'
            elif decision == 'exclude':
                state = 'excluded'
            else:
                # Never hold back a folder with a real camera's photographs in
                # it on a guess. It is copied, and listed for someone to look at.
                def checkpoint():
                    self.gate.check()
                    if self.course_progress:
                        self.course_progress(folder)
                photos = course_material.camera_photographs(long_path(folder), checkpoint=checkpoint)
                state = 'review' if photos is None or photos else 'held'
            verdict = {'path': folder, 'key': key, 'score': judged.score,
                       'reasons': judged.reasons, 'state': state,
                       'camera_photos': photos or 0, 'camera_check_incomplete': photos is None}
            if self.measure_course_folders:
                def checkpoint():
                    self.gate.check()
                    if self.course_progress:
                        self.course_progress(folder)
                verdict['files'], verdict['bytes'], verdict['measurement_truncated'] = _measure_folder(folder, checkpoint)
        self._course_verdicts[folder] = verdict
        return verdict

    def _entertainment_verdict(self, folder, disk_folder, subdirs, files, allowed, volume,
                               listing=None, ahead=None):
        """Films and music in *folder*: (entries to list, names not to copy).

        Worked out once per folder, like a course verdict, so the counting walk
        and the copying walk agree. A decision is kept per folder and category
        under the folder's own key with ``#film`` or ``#music`` added, so it
        follows the drive, and choosing about the songs in a folder says nothing
        about its films.
        """
        if folder in self._entertainment_verdicts:
            return self._entertainment_verdicts[folder]

        def checkpoint():
            self.gate.check()
            if self.course_progress:
                self.course_progress(folder)

        def size_of(path):
            # The walk's directory entries already hold every size; a missing
            # one (a caller without entries) is read the ordinary way.
            size = self._size(disk_folder, os.path.basename(path),
                              (listing.entries.get(os.path.basename(path))
                               if listing is not None else None))
            if size is None:
                raise OSError(f'cannot read the size of {path}')
            return size

        judged = entertainment.judge_folder(
            disk_folder, subdirs, files, kind_of=classify_by_extension, allowed=allowed,
            min_bytes=MIN_MEDIA_BYTES, checkpoint=checkpoint, size_of=size_of,
            tags=((lambda path: ahead.read('tags', path)) if ahead is not None else None),
            listen_to=lambda path, length: self._listen(path, length, volume))

        entries, skip = [], set()
        base = course_material.decision_key(folder, volume)
        for category in entertainment.CATEGORIES:
            verdicts = [v for v in judged.files if v.category == category]
            if not verdicts:
                continue
            held = [v for v in verdicts if v.held]
            spared = [v for v in verdicts if v.personal]
            key = f'{base}#{category}'
            if held:
                decision = db.folder_decision(key)
                state = {'include': 'kept', 'exclude': 'excluded'}.get(decision, 'held')
            else:
                state = 'review'
            artwork = judged.artwork.get(category, [])
            if state in ('held', 'excluded'):
                skip.update(v.name for v in held)
                skip.update(artwork)
            size = sum(v.size for v in held)
            for name in artwork:
                try:
                    size += size_of(os.path.join(disk_folder, name))
                except OSError:
                    pass
            entry = {'path': folder, 'key': key, 'category': category, 'state': state,
                     'score': max(v.score for v in verdicts),
                     'reasons': entertainment.common_reasons(held or spared),
                     'files': len(held) + len(artwork) if held else 0, 'bytes': size if held else 0,
                     'examples': sorted(v.name for v in held)[:3],
                     'spared': len(spared),
                     'spared_reasons': entertainment.common_reasons(spared, limit=3, personal=True),
                     'spared_examples': sorted(v.name for v in spared)[:3]}
            entries.append(entry)
            logged = (folder, category)
            if logged not in self._entertainment_logged:
                self._entertainment_logged.add(logged)
                if held:
                    self.log(f'{category.upper()} {state.upper()} {folder}: {len(held)} files'
                             + (f' and {len(artwork)} artwork' if artwork else '')
                             + f': {"; ".join(entry["reasons"])}')
                for verdict in spared:
                    self.log(f'{category.upper()} SPARED {os.path.join(folder, verdict.name)}: '
                             f'{verdict.personal}; looked like {category}: {"; ".join(verdict.reasons)}')
        self._entertainment_verdicts[folder] = (entries, skip)
        return entries, skip

    def _listen(self, path, length, volume):
        """What an audio file sounds like, heard once per file version and remembered."""
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = course_material.decision_key(short_path(path), volume)
        signature = f'{stat.st_size}:{int(stat.st_mtime)}'
        stored = db.sound_opinion(key, signature)
        if stored is not None:
            return stored
        try:
            heard = entertainment.listen(path, length)
        except Exception as exc:                  # noqa: BLE001 - a guess must never fail a run
            self.log(f'SOUND CHECK SKIPPED {path}: {type(exc).__name__}: {exc}')
            return None
        if heard is not None:
            db.set_sound_opinion(key, signature, *heard)
        return heard

    def _refresh_status_kit(self):
        """Leave a description of the archive on the archive. Never fails the run."""
        try:
            status_kit.write_status_kit(self.destination)
            self.log(f'STATUS written to {os.path.join(self.destination, status_kit.STATUS_PAGE)}')
        except Exception as exc:                  # noqa: BLE001 - the copies are what matter
            self.log(f'STATUS NOT WRITTEN to {self.destination}: {type(exc).__name__}: {exc}')

    def _cleanup_staging(self):
        """Clean up temporary device staging folder once the run finishes."""
        try:
            staging_base = Path(self.destination) / '.ninaivu_staging'
            if staging_base.exists():
                shutil.rmtree(staging_base, ignore_errors=True)
        except Exception:                         # noqa: BLE001
            pass

    def _picture_opinion(self, folder, volume):
        """What the pictures in a borderline folder say, asked once and remembered.

        An answer stored for the same files at the same sizes is used whether or
        not the image model is loaded now, so a run without it agrees with an
        estimate that had it. With no stored answer and no model, the pictures
        add nothing.
        """
        sample = course_material.sample_pictures(long_path(folder))
        if len(sample) < 2:
            return 0, ''
        key = course_material.decision_key(folder, volume)
        signature = course_material.sample_signature(sample, long_path(folder))
        stored = db.picture_opinion(key, signature)
        if stored is not None:
            return stored
        if self.vision is None:
            return 0, ''
        try:
            points, detail = course_material.picture_opinion(self.vision, sample)
        except Exception as exc:                  # noqa: BLE001 - a guess must never fail a run
            self.log(f'PICTURE CHECK SKIPPED {folder}: {type(exc).__name__}: {exc}')
            return 0, ''
        db.set_picture_opinion(key, signature, points, detail,
                               getattr(self.vision, 'model_id', ''))
        return points, detail

    def _read_ahead_for(self, allowed, read_ahead):
        """The read-ahead for one source: which folders and file headers to fetch early."""
        threads = self.read_ahead_threads if read_ahead else 0

        def header_jobs(listing):
            jobs = []
            for name in listing.files:
                found = listing.entries[name]
                kind = classify_by_extension(name)
                if kind is not None and _ext(name) in AMBIGUOUS_EXTS:
                    job = 'sniff'                 # .ts is TypeScript as often as video
                elif kind is None:
                    if (is_sidecar(name) or name.startswith(PARTIAL_PREFIX)
                            or name == ARCHIVE_MARKER
                            or (not self.deep_scan and _ext(name) in NEVER_MEDIA_EXTS)):
                        continue
                    job = 'sniff'
                elif kind == 'audio' and 'audio' in allowed and entertainment.reads_tags(name):
                    job = 'tags'
                else:
                    continue
                size = readahead.entry_size(found)
                if size is None or size >= MIN_MEDIA_BYTES:
                    jobs.append((job, found.path))
            return jobs

        return readahead.ReadAhead(
            threads, skip_dir=lambda name: name.lower() in SKIP_DIRS,
            header_jobs=header_jobs,
            readers={'sniff': sniff_media, 'tags': entertainment.read_tags})

    def _walk(self):
        """
        Yield (root, filename) for every candidate file under every source,
        recursively, once.

        The whole subtree is walked - selecting a drive root scans every folder
        on the drive, at any depth. While counting, worker threads list folders
        and read file headers just ahead of the walk; the walk itself, and every
        decision it makes, stays on this thread and in sorted order. The copying
        walk reads nothing ahead: it would compete with its own copies for the
        source disk, and the counting walk before it has warmed the cache.
        """
        read_ahead = not self._copying
        seen_roots = set()
        # recounted from scratch on each full walk
        self.non_media = 0
        self.filtered_out = 0
        self.filtered_by_kind = {}
        self.included_by_kind = {}
        self.too_small = 0
        self.last_kind = None
        self.skipped_system_dirs = 0
        self.course_folders = []
        self.entertainment = []
        self.set_aside_files = 0
        kept_course_roots = set()     # descendants of a kept course are not re-judged
        neighbourhoods = {}           # folder -> course_neighbourhood of its children
        lesson_parents = set()        # folders whose children are numbered lessons
        self.unreadable_dirs = []
        self._unreadable_seen = set()
        self.device_shortfalls = []
        self.deepest = 0

        # The archive itself must never be read back in as a source. Pruning it
        # here - rather than refusing the whole job - is what lets the archive
        # live on a drive you are also scanning: source C:\ with destination
        # C:\Master walks the whole drive and steps around C:\Master.
        # Both sides go through normalise(), which strips the \\?\ prefix, so a
        # folder arriving from the long-path walk still matches a plain-form
        # destination. Comparing the raw strings made this check silently answer
        # "no" on Windows and never prune anything.
        dest_norm = normalise(self.destination)
        dest_inside = dest_norm.rstrip(os.sep) + os.sep

        def is_destination(path):
            if '.ninaivu_staging' in path:
                return False
            return normalise(path) == dest_norm or is_within(path, self.destination)

        try:
            from ..utils import devices
        except Exception:
            devices = None

        for entry in self.source_entries:
            source, allowed = entry['path'], entry['types']
            if is_destination(source):
                # The whole source sits inside the archive - nothing to scan.
                self.pruned_destination += 1
                self.log(f'SKIPPED SOURCE {source}: it is the destination archive '
                         f'or lives inside it')
                continue

            if devices and devices.looks_like_device_path(source):
                if self._copying and self.mode != MODE_COPY:
                    # A dry run or a verify reads each file where it lies, and
                    # a phone's files lie nowhere a file can be opened. Staging
                    # them would write to the destination, which neither may.
                    self.log(f'SKIPPED SOURCE {source}: a phone or camera is only '
                             f'read by a copy run')
                    continue
                if self._copying:
                    device_slug = re.sub(r'[^\w\-_.]', '_', source).strip('_')
                    staging_dir = Path(self.destination) / '.ninaivu_staging' / device_slug
                    staging_dir.mkdir(parents=True, exist_ok=True)
                    self.log(f'STAGING from device {source} to {staging_dir}…')
                    if self.job_id:
                        db.update_job(self.job_id, phase='staging',
                                      message=f'staging media from {source}…')

                    def _stage_progress(copied, bytes_done, current_name):
                        self.gate.check()
                        if self.job_id:
                            db.update_job(self.job_id,
                                          message=f'staging {current_name} ({copied} files, {_human(bytes_done)})')

                    device_name = devices.split_device_path(source)[0] or source

                    # Bound here: this runs inside copy_out below, for this device.
                    def _stage_waiting(where, device_name=device_name):
                        self.gate.check()
                        self.log(f'DEVICE AWAY {device_name} stopped answering while '
                                 f'{where} was being read - waiting for it; unlock '
                                 f'it or plug it back in to carry on')
                        if self.job_id:
                            db.update_job(self.job_id,
                                          message=f'waiting for {device_name} - unlock '
                                                  f'it or plug it back in to carry on')

                    def _already_archived(landing, exact_size):
                        # A staged file is removed once archived, so without
                        # this every Start after a phone locked fetched the
                        # whole camera roll again, only to find it done.
                        size = db.finished_size(short_path(str(landing)))
                        return size is not None and exact_size in (None, size)

                    res = devices.copy_out(
                        source, staging_dir,
                        progress=_stage_progress,
                        waiting=_stage_waiting,
                        already_have=_already_archived,
                        should_stop=lambda: self.gate.is_cancelled,
                        preserve_structure=True)
                    self.gate.check()
                    self.log(f'STAGED {res["copied"]} files ({_human(res["bytes"])}) '
                             f'from {source} ({res["skipped"]} already staged, {res["failed"]} failed)')
                    # A phone that locked itself part-way used to leave the run
                    # saying "complete" with most of the camera roll still on
                    # it. Said plainly instead, and the run is INCOMPLETE.
                    shortfall = []
                    if res.get('device_lost'):
                        shortfall.append(f'{device_name} stopped answering part-way '
                                         f'through and did not come back, so the files '
                                         f'after that point were not copied off it')
                    if res['failed']:
                        shortfall.append(f'{res["failed"]} files could not be copied '
                                         f'off it - see the run log')
                    if shortfall:
                        detail = '; '.join(shortfall)
                        self.device_shortfalls.append((source, detail))
                        self.log(f'DEVICE INCOMPLETE {source}: {detail}. Press Start '
                                 f'again with it unlocked; what was copied is stepped over')
                    walk_root = long_path(str(staging_dir))
                    source = str(staging_dir)
                else:
                    for dev_entry in devices.walk(source, should_stop=lambda: self.gate.is_cancelled):
                        self.gate.check()
                        name = dev_entry['name']
                        kind = classify_by_extension(name)
                        if kind is None or kind not in allowed:
                            if kind is not None:
                                self.filtered_out += 1
                                self.filtered_by_kind[kind] = self.filtered_by_kind.get(kind, 0) + 1
                            else:
                                self.non_media += 1
                            continue
                        size = dev_entry.get('size') or 0
                        if size > 0 and size < MIN_MEDIA_BYTES:
                            self.too_small += 1
                            continue
                        self.last_size = size
                        self.last_kind = kind
                        self.included_by_kind[kind] = self.included_by_kind.get(kind, 0) + 1
                        parent_path = os.path.dirname(dev_entry['path'])
                        yield parent_path, name
                    continue
            # Walk through the long-path form on Windows. Without it, os.scandir
            # fails on any folder past ~260 characters, and the deepest,
            # messiest parts of a drive are the ones that silently go missing.
            walk_root = long_path(source)
            volume = _volume_identity(source)
            base_depth = walk_root.rstrip(os.sep).count(os.sep)
            ahead = self._read_ahead_for(allowed, read_ahead)
            # Each folder travels with its real, normalised location. A plain
            # subfolder's is its parent's plus its name; only a junction or a
            # symlink has to be asked, so the destination and loop checks cost
            # a string comparison instead of a disk lookup per folder.
            stack = [(walk_root, normalise(source))]
            try:
                while stack:
                    root, canon = stack.pop()
                    listing = ahead.listing(root)
                    if listing.error is not None:
                        self._on_walk_error(listing.error)
                        continue

                    depth = root.rstrip(os.sep).count(os.sep) - base_depth
                    if depth > self.deepest:
                        self.deepest = depth

                    keep, canons = [], {}
                    for d in listing.dirs:
                        if d.lower() in SKIP_DIRS:
                            self.skipped_system_dirs += 1
                            continue
                        full = os.path.join(root, d)
                        if d in listing.junctions or d in listing.links:
                            child = normalise(full)
                        else:
                            child = os.path.join(canon, os.path.normcase(d))
                        if (child == dest_norm or child.startswith(dest_inside)) and '.ninaivu_staging' not in child:
                            # This is the archive (or a folder inside it). Step
                            # around it: reading the archive back in as a source
                            # would re-ingest every file it already holds.
                            self.pruned_destination += 1
                            self.log(f'SKIPPED {short_path(full)}: destination archive')
                            ahead.discard(full)
                            continue
                        keep.append(d)
                        canons[d] = child
                    # Sorted, so the walk order is reproducible. Without this, which
                    # of two byte-identical photos becomes the archived copy and
                    # which is logged as the duplicate depends on raw directory
                    # order, so two runs over the same drives can disagree and a dry
                    # run cannot promise to predict the real run.
                    dirs = sorted(keep)
                    files = sorted(listing.files)

                    if canon in seen_roots:       # junction / symlink loop guard
                        ahead.discard(root)
                        continue
                    seen_roots.add(canon)

                    plain_root = short_path(root)
                    neighbourhoods[plain_root] = course_material.course_neighbourhood(dirs)
                    # A numbered lesson is part of the course above it, not a course of
                    # its own: judged alone, "03 Lesson 3" among its siblings looks like
                    # one, and a course chosen as the source would be held back lesson
                    # by lesson.
                    is_lesson = os.path.dirname(plain_root) in lesson_parents
                    if course_material.numbered_lessons(dirs):
                        lesson_parents.add(plain_root)
                    in_kept_course = _within_any(canon, kept_course_roots, normalised=True)

                    # The folder a source names is the one somebody chose, so it is
                    # taken as asked; everything below it is judged.
                    if depth > 0 and not is_lesson and not in_kept_course:
                        verdict = self._course_verdict(
                            plain_root, list(dirs), files,
                            neighbourhoods.get(os.path.dirname(plain_root), 0), volume)
                        if verdict is not None:
                            self.course_folders.append(verdict)
                            if plain_root not in self._course_logged:
                                self._course_logged.add(plain_root)
                                self.log(f'COURSE MATERIAL {verdict["state"].upper()} '
                                         f'{plain_root}: {"; ".join(verdict["reasons"])}'
                                         + (f' ({verdict["camera_photos"]} camera photos)'
                                            if verdict['camera_photos'] else ''))
                            if verdict['state'] in ('held', 'excluded'):
                                ahead.discard(root)   # set aside whole: not walked into
                                continue
                            kept_course_roots.add(canon)
                            in_kept_course = True

                    # Films and songs are judged file by file, so the voice note
                    # beside a song is never held back with it. A kept course is
                    # taken whole, as its keeper chose.
                    set_aside = ()
                    if not in_kept_course:
                        found, set_aside = self._entertainment_verdict(
                            plain_root, root, list(dirs), files, allowed, volume,
                            listing=listing, ahead=ahead)
                        self.entertainment.extend(found)

                    for name in files:
                        if name.startswith(PARTIAL_PREFIX):
                            continue
                        if name in set_aside:
                            self.set_aside_files += 1
                            continue
                        if self._wanted(plain_root, name, allowed,
                                        listing.entries.get(name), ahead):
                            yield plain_root, name

                    ahead.finished(root)
                    for d in reversed(dirs):
                        if d not in listing.links:
                            stack.append((os.path.join(root, d), canons[d]))
            finally:
                ahead.close()

    def note_prior_progress(self):
        """Read what the archive already holds, before this run changes it.

        Taken once, at the start, and never updated: the point is to say what
        was true when Start was pressed. Reading it later would fold this run's
        own work back into "already done" and the number would climb, which is
        exactly the confusion it exists to remove.
        """
        try:
            self.resumed_from = db.finished_count()
        except Exception:                      # noqa: BLE001
            # A count that cannot be taken is not worth failing a run over.
            self.resumed_from = 0
        return self.resumed_from

    @staticmethod
    def _finished_among(paths):
        try:
            return db.finished_among(paths)
        except Exception:                      # noqa: BLE001
            # Only the estimate leans on this; not worth failing a run over.
            return 0

    def preflight(self):
        """
        Count candidates and measure how much data is actually outstanding, so
        the UI can show real progress and so we can refuse a job that will not
        fit before it fills the volume halfway through.
        """
        count = 0
        outstanding = 0
        finished = 0
        batch = []
        for root, name in self._walk():
            self.gate.check()
            count += 1
            outstanding += self.last_size
            batch.append(os.path.join(root, name))
            if count % 500 == 0:
                finished += self._finished_among(batch)
                batch = []
                db.update_job(self.job_id, total_files=count)
        finished += self._finished_among(batch)
        self.finished_in_run = finished
        self.total_files = count
        self.total_bytes = outstanding
        db.update_job(self.job_id, total_files=count, total_bytes=outstanding)
        return count

    def check_capacity(self):
        """
        Refuse up front rather than dying at 80%. Only the bytes not already
        archived are counted, so resuming a large job does not trip the check.
        """
        already = db.bytes_already_archived()
        needed = max(0, self.total_bytes - already)
        ok, free, required = check_free_space(self.destination, needed)
        if not ok:
            raise RuntimeError(
                f'Not enough space on the destination volume. '
                f'About {_human(required)} is needed (including headroom) but only '
                f'{_human(free)} is free. Free up space or choose another volume.')
        return free, needed

    # ---------------- temp file hygiene ----------------

    #: How a copy run can end without leaving a temporary behind: every copy
    #: that finishes, stops or fails removes its own. Anything else - a run
    #: still marked running after the app closed, or one that lost a drive -
    #: may have left one mid-file.
    _CLEAN_ENDINGS = ('completed', 'stopped', 'failed')

    def _partials_possible(self):
        """Whether an earlier run could have left temporaries in this destination.

        Sweeping lists every folder in the archive, which on a large archive on
        a USB disk is minutes before the first copy. It is only worth that when
        a run here ended uncleanly since the last sweep - or when this database
        has no record of copying here at all, as after its state was reset.
        """
        if self.job_id is None:
            return True
        destination = normalise(self.destination)
        swept_through = int(db.get_config(f'partials-swept:{destination}', '0') or 0)
        copies = [job for job in db.jobs_before(self.job_id)
                  if job['mode'] == MODE_COPY
                  and normalise(job['destination']) == destination]
        if not copies:
            return True
        return any(job['id'] > swept_through and job['state'] not in self._CLEAN_ENDINGS
                   for job in copies)

    def _unclean_folders(self):
        """The folders to look in for leftovers, or ``None`` for all of them.

        Jobs record every folder they make a temporary in (`job_folders`), so
        after an unclean ending only those folders can hold one. A job from
        before that record existed could have left one anywhere, and so could
        an archive this database has no history of; both mean the whole walk.
        """
        if self.job_id is None:
            return None
        destination = normalise(self.destination)
        swept_through = int(db.get_config(f'partials-swept:{destination}', '0') or 0)
        tracked_from = int(db.get_config(f'temp-folders-tracked:{destination}', '0') or 0)
        copies = [job for job in db.jobs_before(self.job_id)
                  if job['mode'] == MODE_COPY
                  and normalise(job['destination']) == destination]
        if not copies:
            return None
        unclean = [job['id'] for job in copies
                   if job['id'] > swept_through and job['state'] not in self._CLEAN_ENDINGS]
        if not tracked_from or any(job_id < tracked_from for job_id in unclean):
            return None
        return db.job_folders(unclean)

    def _mark_folders_tracked(self):
        """From this job on, every temporary's folder is on record here."""
        if self.job_id is None:
            return
        key = f'temp-folders-tracked:{normalise(self.destination)}'
        if not db.get_config(key):
            db.set_config(key, str(self.job_id))

    def sweep_partials(self):
        """
        Remove our own aborted temporaries from a previous run. These are files
        this module created and never renamed into place; they are not user data,
        so removing them does not breach 'nothing is deleted'.
        """
        removed = 0
        if not os.path.isdir(long_path(self.destination)):
            self._mark_folders_tracked()
            return 0
        if not self._partials_possible():
            self._mark_folders_tracked()
            return 0
        swept_through = (self.job_id or 1) - 1
        folders = self._unclean_folders()
        self._mark_folders_tracked()
        if folders is not None:
            # Only where an interrupted job was writing. Listing a few folders
            # is instant; listing the archive was minutes before the first copy.
            for folder in folders:
                try:
                    names = os.listdir(long_path(folder))
                except OSError:
                    continue
                for name in names:
                    if name.startswith(PARTIAL_PREFIX):
                        try:
                            os.remove(long_path(os.path.join(folder, name)))
                            removed += 1
                        except OSError:
                            pass
            if self.job_id is not None:
                db.set_config(f'partials-swept:{normalise(self.destination)}',
                              str(swept_through))
            return removed
        # Long-path form here too: a deep archive folder would otherwise fail to
        # list on Windows and its abandoned temporaries would never be cleared.
        for root, _dirs, files in os.walk(long_path(self.destination)):
            for name in files:
                if name.startswith(PARTIAL_PREFIX):
                    try:
                        os.remove(long_path(os.path.join(short_path(root), name)))
                        removed += 1
                    except OSError:
                        pass
        if self.job_id is not None:
            db.set_config(f'partials-swept:{normalise(self.destination)}', str(swept_through))
        return removed

    def _record_unreadable_dirs(self):
        """
        Put every folder that could not be listed into the report as an error,
        so an unscanned subtree is something the user can see and retry rather
        than a hole in an archive that claims to be complete.
        """
        for path, message in self.unreadable_dirs:
            try:
                db.claim_file(path, os.path.basename(path.rstrip(os.sep)) or path,
                              0, 0, self.job_id, destination_root=self.destination)
                db.set_status(path, 'error',
                              error=f'folder could not be listed, so nothing '
                                    f'inside it was scanned: {message}')
            except Exception as exc:  # noqa: BLE001 - keep the run; keep the fact
                # This exists so an unscanned subtree is visible. If the report
                # cannot take it, the run log still must.
                self.log(f'UNREADABLE FOLDER NOT RECORDED IN REPORT {path} '
                         f'({message}): {type(exc).__name__}: {exc}')

    def _discard(self, tmp):
        try:
            if os.path.exists(long_path(tmp)):
                os.remove(long_path(tmp))
        except OSError:
            pass

    # ---------------- the copy ----------------

    def _copy_and_hash(self, src, tmp):
        """
        Stream source -> temp, hashing what we read as we go. Interruptible
        between chunks. Returns (digest_of_bytes_read, byte_count).
        """
        h = hashlib.sha256()
        total = 0
        last = 0
        with open(long_path(src), 'rb') as fi, open(long_path(tmp), 'wb') as fo:
            while True:
                self.gate.check()
                self.pacer.checkpoint(self.gate, last)
                chunk = fi.read(CHUNK)
                if not chunk:
                    break
                last = len(chunk)
                h.update(chunk)
                fo.write(chunk)
                total += len(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        return h.hexdigest(), total

    def _unique_path(self, folder, filename, src_hash, planning=False,
                     src_size=None, companions=()):
        """
        Resolve a name collision in the archive.

        If something already sits at the target path we hash it. Identical bytes
        mean we already hold this photo, so the incoming file is a duplicate
        rather than something needing a _1 suffix. Only genuinely different
        files get suffixed.

        When `planning`, a path another dry-run prediction has already claimed
        also counts as taken - a dry run writes nothing, so the filesystem alone
        cannot tell it that two same-named photos are about to collide.

        `companions` are the source files that share this one's stem (a live
        photo's still and clip, a RAW and its JPEG). A suffix is only taken when
        it is free for them too, so both halves arrive at the same one without
        knowing about each other: the still and the clip each skip exactly the
        same names. Suffixed separately, IMG_1.HEIC could land as IMG_1_1.HEIC
        beside a stranger's IMG_1.MOV, and Ninaivu paired the wrong clip.

        Returns (path, existing_is_identical).
        """
        stem, ext = os.path.splitext(filename)
        companion_hashes = {}
        counter = 0
        while True:
            self.gate.check()
            name = filename if counter == 0 else f'{stem}_{counter}{ext}'
            candidate = os.path.join(folder, name)

            if os.path.exists(long_path(candidate)):
                if self._same_bytes(candidate, src_hash, src_size):
                    return candidate, True
            elif not self._free_for_companions(folder, counter, companions,
                                               planning, companion_hashes):
                pass
            elif not (planning and db.path_is_planned(candidate)):
                if planning:
                    return candidate, False
                # A free name is only free until another worker takes it, and
                # nothing exists on disk between choosing the name and the
                # rename at the end of the copy. Claim it here so two workers
                # cannot both decide IMG_1234.jpg is available.
                with self._decide_lock:
                    if _claim_key(candidate) not in self._claimed:
                        self._claimed.add(_claim_key(candidate))
                        return candidate, False

            counter += 1
            if counter > 9999:
                raise RuntimeError(f'could not find a free name for {filename}')

    def _companions(self, src_path, name):
        """The other media files beside *src_path* with the same stem.

        Listed once per source folder (the source is read-only for the run)
        and whatever the run's media types, so a run that archives only the
        pictures picks the same suffix a later run for the videos will.
        """
        src_dir = os.path.dirname(src_path)
        groups = self._companion_cache.get(src_dir)
        if groups is None:
            groups = {}
            try:
                names = os.listdir(long_path(src_dir))
            except OSError:
                names = []
            for other in names:
                if is_supported_media(other) and not is_sidecar(other):
                    key = os.path.splitext(other)[0].casefold()
                    groups.setdefault(key, []).append(other)
            self._companion_cache[src_dir] = groups
        key = os.path.splitext(name)[0].casefold()
        return [(os.path.join(src_dir, other), other)
                for other in groups.get(key, ()) if other != name]

    def _free_for_companions(self, folder, counter, companions, planning,
                             hashes):
        """Whether suffix *counter* is free for every companion as well.

        A name is free for a companion when nothing is there, or when what is
        there is that companion itself: already archived (or, in a dry run,
        already predicted) to exactly that name, or the same bytes.
        """
        for comp_src, comp_name in companions:
            comp_stem, comp_ext = os.path.splitext(comp_name)
            name = comp_name if counter == 0 else f'{comp_stem}_{counter}{comp_ext}'
            candidate = os.path.join(folder, name)
            if os.path.exists(long_path(candidate)):
                if db.path_is_recorded_for(candidate, comp_src):
                    continue
                try:
                    if comp_src not in hashes:
                        hashes[comp_src] = (hash_file(comp_src, self.gate),
                                            os.path.getsize(long_path(comp_src)))
                    if self._same_bytes(candidate, *hashes[comp_src]):
                        continue
                except OSError:
                    pass
                return False
            if planning and db.path_is_planned(candidate) \
                    and not db.path_is_recorded_for(candidate, comp_src):
                return False
            with self._decide_lock:
                if _claim_key(candidate) in self._claimed:
                    return False
        return True

    def _same_bytes(self, candidate, src_hash, src_size):
        """
        Is the file already at `candidate` byte-identical to the one we are
        about to write?

        Answered as cheaply as possible, because a folder full of same-named
        photos asks this once per collision and the naive version re-reads every
        neighbour every time:

          1. Different size - impossible to match, no read at all.
          2. Size matches and we archived it ourselves - the digest is already
             on record, so still no read.
          3. Otherwise - hash it, the honest but expensive answer.

        The recorded digest is only trusted when the file's size still matches
        what was recorded, so a file replaced in the archive is re-hashed rather
        than assumed. A file edited in place to exactly the same length would
        slip through, which is precisely what Audit archive exists to catch.
        """
        try:
            actual_size = os.path.getsize(long_path(candidate))
        except OSError:
            return False                   # unreadable neighbour - suffix past it

        if src_size is not None and actual_size != src_size:
            return False

        record = db.archived_record(candidate)
        if record and record['size'] == actual_size:
            return record['hash'] == src_hash

        try:
            return hash_file(candidate, self.gate) == src_hash
        except OSError:
            return False

    def _claim_or_wait(self, digest, dedup_statuses, exclude_path, timeout=900):
        """Become the one worker archiving these bytes, or find who already is.

        Returns the duplicate row to record, or None if this worker owns the
        copy. Sequentially this is a no-op; in parallel it is what stops two
        byte-identical photos both landing because neither had verified yet
        when the other looked.
        """
        while True:
            with self._decide_lock:
                # `exclude_path` must be a real path: the query compares with
                # `source_path != ?`, and in SQL `!= NULL` is never true, so
                # passing None quietly matched nothing at all and every
                # duplicate slipped through.
                dup = db.find_verified_duplicate(
                    digest, exclude_path, dedup_statuses,
                    destination_root=self.destination)
                if dup:
                    return dup
                waiting = self._inflight.get(digest)
                if waiting is None:
                    self._inflight[digest] = threading.Event()
                    return None                  # ours to copy
            # Someone else is copying these exact bytes. Wait for them, then
            # look again — they will have verified, and we record a duplicate.
            if not waiting.wait(timeout):
                return None                      # gave up waiting; copy it too

    def _await_inflight(self, digest, timeout=900):
        """Wait for another worker copying these exact bytes to finish.

        Only ever waits on a *different* worker's copy of an identical file,
        which is rare and short. The timeout exists so a worker that dies
        holding the flag cannot wedge the run — the cost of giving up is one
        redundant copy, not a hang.
        """
        with self._decide_lock:
            waiting = self._inflight.get(digest)
        if waiting is not None:
            waiting.wait(timeout)

    def _unclaim(self, path):
        """Give a reserved archive name back.

        A name is reserved between choosing it and the rename that fills it.
        If the file turns out to be a duplicate, or the copy fails, the name
        must become available again or every later photo with that name gets
        an unnecessary _1 suffix — and a dry run would then stop predicting
        what the real run does.
        """
        if not path:
            return
        with self._decide_lock:
            self._claimed.discard(_claim_key(path))

    def _release_inflight(self, digest):
        """Let anyone waiting on these bytes carry on."""
        if not digest:
            return
        with self._decide_lock:
            waiting = self._inflight.pop(digest, None)
        if waiting is not None:
            waiting.set()

    def _copy_sidecars(self, src_path, final_path):
        """
        Carry .xmp / .aae / .thm / .json companions across next to their parent,
        renamed to match if the parent was suffixed. Best-effort and never fatal:
        losing a sidecar is bad, failing the photo because of one is worse.
        """
        if self.mode != MODE_COPY:
            return
        src_dir = os.path.dirname(src_path)
        src_base = os.path.basename(src_path)
        src_stem = os.path.splitext(src_base)[0]
        final_stem = os.path.splitext(os.path.basename(final_path))[0]
        final_dir = os.path.dirname(final_path)

        # Listing the folder per file made this O(files x folder size): a card
        # with 5,000 photos in one directory listed 5,000 names 5,000 times,
        # and the only entries that ever matter are the handful of sidecars.
        # Cache the sidecars per folder instead - the source is read-only, so
        # the answer cannot change underneath us during a run.
        neighbours = self._sidecar_cache.get(src_dir)
        if neighbours is None:
            try:
                neighbours = [n for n in os.listdir(long_path(src_dir))
                              if is_sidecar(n)]
            except OSError:
                neighbours = []
            self._sidecar_cache[src_dir] = neighbours
        if not neighbours:
            return
        for name in neighbours:
            stem = os.path.splitext(name)[0]
            # Matches IMG_1.JPG.xmp, IMG_1.xmp and IMG_1.JPG.supplemental-metadata.json
            if not (stem == src_base or stem == src_stem
                    or stem.startswith(src_base + '.')):
                continue
            out_name = name if src_stem == final_stem \
                else name.replace(src_stem, final_stem, 1)
            target = os.path.join(final_dir, out_name)
            if os.path.exists(long_path(target)):
                continue
            try:
                shutil.copy2(long_path(os.path.join(src_dir, name)), long_path(target))
                self.sidecars_copied += 1
            except Exception as exc:  # noqa: BLE001 - never fail the photo over it
                # Still never fatal, but no longer silent: a sidecar holds the
                # edits and captions somebody made, and the summary is where
                # they find out one did not arrive.
                self.sidecars_failed += 1
                self.log(f'SIDECAR NOT COPIED {os.path.join(src_dir, name)}: '
                         f'{type(exc).__name__}: {exc}')

    # ---------------- per-file work ----------------

    def process_file(self, src_path, name):
        st = os.stat(long_path(src_path))
        size, mtime = st.st_size, st.st_mtime
        dry = self.mode == MODE_DRY_RUN

        prior = db.claim_file(src_path, name, size, mtime, self.job_id,
                              destination_root=self.destination)
        if prior in db.TERMINAL_STATUSES:
            return 'already-done'

        if size == 0:
            # Registered first, so it is visible in the report. The old code
            # updated a row it had never inserted, and the file vanished.
            db.set_status(src_path, 'plan-skip' if dry else 'skipped',
                          error='zero-byte file')
            return 'skipped'

        dt, date_src = capture_date(src_path, st=st)
        exif_str = dt.strftime('%Y-%m-%d %H:%M:%S') if dt else None

        # Cheap dedup pre-filter: a byte-identical duplicate must match on size.
        # When no verified file shares this size, no duplicate is possible, so we
        # skip the standalone hashing pass and hash during the copy instead -
        # one read of the file rather than two.
        # During a dry run nothing ever reaches 'verified', so predictions have
        # to dedupe against earlier predictions as well as against real history.
        # The size check is scoped to this archive like the duplicate check it
        # stands in for: a same-sized file verified into some other archive is
        # not a duplicate here, so it must not cost this file a second read.
        dedup_statuses = ('verified', 'planned') if dry else db.DEDUP_STATUSES

        src_hash = None
        claimed = False            # whether this worker holds _inflight[src_hash]
        if dry or db.size_is_known(size, destination_root=self.destination):
            src_hash = hash_file(src_path, self.gate)
            # With several workers in flight, two byte-identical photos can be
            # hashed at the same moment and neither would see the other in the
            # database yet, so both would be copied. Waiting here for the one
            # already in flight makes the answer identical to the sequential
            # one — which is what lets a dry run predict a real run.
            if dry:
                dup = db.find_verified_duplicate(src_hash, src_path, dedup_statuses,
                                                 destination_root=self.destination)
            else:
                dup = self._claim_or_wait(src_hash, dedup_statuses, src_path)
                claimed = dup is None
            if dup and dup['source_path'] == src_path:
                dup = None
            if dup:
                db.set_status(src_path, 'plan-duplicate' if dry else 'duplicate',
                              file_hash=src_hash, exif_date=exif_str,
                              date_source=date_src,
                              duplicate_of=dup['source_path'],
                              destination_path=dup['destination_path'])
                self.log(f'DUPLICATE {src_path} == {dup["source_path"]}')
                return 'plan-duplicate' if dry else 'duplicate'

        folder = target_folder(self.destination, dt, src_path)
        if dt is None:
            # An earlier version filed every undated file in the flat folder.
            # The same bytes already there under this name are this file's
            # copy, not a reason to make a second one a level down.
            flat = target_folder(self.destination, None)
            earlier = os.path.join(flat, name)
            if folder != flat and os.path.isfile(long_path(earlier)) \
                    and os.path.getsize(long_path(earlier)) == size:
                if src_hash is None:
                    src_hash = hash_file(src_path, self.gate)
                if self._same_bytes(earlier, src_hash, size):
                    folder = flat

        # ---- dry run stops here: decide, record, touch nothing -------------
        if dry:
            planned, identical = self._unique_path(
                folder, name, src_hash, planning=True, src_size=size,
                companions=self._companions(src_path, name))
            status = 'plan-duplicate' if identical else 'planned'
            db.set_status(src_path, status, file_hash=src_hash, exif_date=exif_str,
                          date_source=date_src, destination_path=planned,
                          duplicate_of=planned if identical else None)
            self.log(f'PLAN {status} {src_path} -> {planned}')
            return status

        # The bytes may already be claimed as this worker's to copy, and the
        # try/finally below that lets them go only starts further down. A
        # folder that cannot be made — a full or vanished drive — or a failed
        # status write in between used to leave the claim held for the rest of
        # the run, so every identical photo after it sat out the whole timeout.
        try:
            if folder not in self._made_dirs:
                # Recorded before the folder joins the set: another worker only
                # skips this block once it is there, so no temporary is ever
                # made in a folder the database does not yet list for this job.
                db.note_job_folder(self.job_id, folder)
                os.makedirs(long_path(folder), exist_ok=True)
                self._made_dirs.add(folder)
            with self._count_lock:
                self.counter += 1
                serial = self.counter
            # The thread id is in the name as well as the counter: two workers
            # must never share a temp file, and a counter alone relies on the
            # increment above never being read torn.
            tmp = os.path.join(
                folder,
                f'{PARTIAL_PREFIX}{os.getpid()}-{threading.get_ident()}-{serial}.tmp')
            db.set_status(src_path, 'copying', exif_date=exif_str, date_source=date_src)
        except BaseException:
            if claimed:
                self._release_inflight(src_hash)
            raise

        claimed_digest = src_hash
        reserved = None
        try:
            read_hash, written = self._copy_and_hash(src_path, tmp)

            if src_hash is not None and read_hash != src_hash:
                raise IOError('source read inconsistently: the file changed under us '
                              'or the drive is returning unstable data')
            if written != size:
                # A file still being written when the walk reached it — a
                # camera-sync client mid-copy. Read to its end it hashes fine,
                # so the copy would have verified at the wrong length and been
                # recorded at the length the walk saw. Error status is not
                # terminal: it is tried again next run, whole.
                raise IOError(f'source changed during the copy ({size} bytes when '
                              f'found, {written} read); it will be tried again')
            first_hash_known = src_hash is not None
            src_hash = read_hash

            # Late dedup check: covers the file that was not hashed before the
            # copy, because at that moment no verified file shared its size and
            # so no duplicate was possible. Two things can have changed since:
            # an identical photo may have verified, or — with several workers —
            # may be landing right now. `_claim_or_wait` answers both, and
            # sequentially it is the same cheap lookup this always was.
            if not first_hash_known:
                claimed_digest = src_hash
                dup = self._claim_or_wait(src_hash, dedup_statuses, src_path)
                if dup is not None and dup['source_path'] == src_path:
                    dup = None
            else:
                dup = db.find_verified_duplicate(
                    src_hash, src_path, destination_root=self.destination)
            if dup:
                self._discard(tmp)
                if '.ninaivu_staging' in src_path:
                    try:
                        os.remove(long_path(src_path))
                    except OSError:
                        pass
                db.set_status(src_path, 'duplicate', file_hash=src_hash,
                              exif_date=exif_str, date_source=date_src,
                              duplicate_of=dup['source_path'],
                              destination_path=dup['destination_path'])
                self.log(f'DUPLICATE {src_path} == {dup["source_path"]}')
                return 'duplicate'

            final, identical = self._unique_path(
                folder, name, src_hash, src_size=size,
                companions=self._companions(src_path, name))
            reserved = final
            if identical:
                self._discard(tmp)
                if '.ninaivu_staging' in src_path:
                    try:
                        os.remove(long_path(src_path))
                    except OSError:
                        pass
                # These exact bytes are already at the exact path we were about
                # to write. Either a previous run of this tool put them there, or
                # another source file did. If nothing else claims that path, this
                # file IS the archive's copy - adopt it as verified rather than
                # calling it a duplicate of a file nothing has a record of.
                if db.path_is_claimed(final, src_path):
                    db.set_status(src_path, 'duplicate', file_hash=src_hash,
                                  destination_path=final, duplicate_of=final)
                    return 'duplicate'
                db.set_status(src_path, 'verified', file_hash=src_hash,
                              dest_hash=src_hash, destination_path=final)
                self._copy_sidecars(src_path, final)
                self.log(f'ADOPTED {src_path} -> {final}')
                return 'adopted'

            # Timestamps are stamped onto the temp file before the rename, so the
            # file at the final path is correct the instant it appears there.
            try:
                shutil.copystat(long_path(src_path), long_path(tmp))
            except OSError:
                pass

            os.replace(long_path(tmp), long_path(final))
            db.set_status(src_path, 'copied', file_hash=src_hash,
                          destination_path=final)
            db.add_bytes(self.job_id, written)

            # I-2: re-read from disk. Hashing the bytes we wrote only proves we
            # read the source correctly; re-reading proves they actually landed.
            dest_hash = hash_file(final, self.gate)
            if dest_hash != src_hash:
                # The bad copy must not stay at ``final`` looking like a
                # photograph: the retry would find the name taken, land the good
                # copy beside it as ``name_1`` and point the record there, and
                # nothing — not the audit, not the sweep — would look at the
                # corrupt file again while the library indexed it as real. It
                # is this module's own unverified output, like a ``.partial``,
                # and is discarded like one: first back under that prefix, so
                # that if the remove itself fails the sweep still knows it for
                # a leftover, then removed. The name is free for the retry.
                quarantine = os.path.join(
                    folder, f'{PARTIAL_PREFIX}corrupt-{os.getpid()}-{serial}.tmp')
                try:
                    os.replace(long_path(final), long_path(quarantine))
                except OSError:
                    quarantine = final
                else:
                    self._discard(quarantine)
                db.set_status(src_path, 'error', dest_hash=dest_hash,
                              destination_path=None,
                              error='verification failed: the archived copy did '
                                    'not match the source hash and was discarded')
                self.log(f'CORRUPT {src_path} -> {final} '
                         f'expected {src_hash} got {dest_hash}; the copy was discarded')
                return 'error'

            set_windows_ctime(final, st.st_mtime)
            # A photo somebody hid must not become a visible one just because
            # it was copied into a plainly named YYYY/MM/DD folder. Marking the
            # copy is what makes the library index it as hidden in turn.
            if source_is_hidden(src_path, self._hidden_folders):
                mark_hidden(final)
                self.hidden_carried += 1
                self.log(f'HIDDEN {src_path} -> {final} (source was hidden)')
            db.set_status(src_path, 'verified', dest_hash=dest_hash,
                          destination_path=final)
            self._copy_sidecars(src_path, final)
            if '.ninaivu_staging' in src_path:
                try:
                    os.remove(long_path(src_path))
                except OSError:
                    pass
            self.log(f'VERIFIED {src_path} -> {final} sha256={src_hash}')
            return 'verified'

        except Cancelled:
            # I-4: the temp never became a real archive file, so nothing is
            # half-written at a path anyone will trust.
            self._discard(tmp)
            db.set_status(src_path, 'pending')
            raise
        except Exception as exc:
            self._discard(tmp)
            self._raise_if_disconnected()
            db.set_status(src_path, 'error', error=f'{type(exc).__name__}: {exc}')
            self.log(f'ERROR {src_path}: {type(exc).__name__}: {exc}')
            return 'error'
        finally:
            # However this ended, anyone waiting on these bytes must be let go,
            # or an identical photo later in the run waits out the timeout — and
            # a name reserved but never filled must go back, or later photos are
            # suffixed for no reason.
            self._release_inflight(claimed_digest)
            self._release_inflight(src_hash)
            # Released whether or not the rename happened. Once it has, the
            # name is held by a real file and ``os.path.exists`` answers for
            # it; keeping the reservation as well only grew ``_claimed`` by one
            # path for every file copied, for the whole run — tens of
            # megabytes across a large consolidation, answering no question.
            # And if the copy failed verification and was moved away again,
            # the name really is free.
            self._unclaim(reserved)

    # ---------------- the copy loop ----------------

    def _one_file(self, path, name):
        """Archive one file, turning any failure into a recorded error.

        Identical whether it runs on this thread or a worker, so a run with one
        worker takes exactly the same path as a run with eight.
        """
        completed = False
        stepped_over = False
        try:
            stepped_over = self.process_file(path, name) == 'already-done'
            completed = True
        except Cancelled:
            raise
        except DriveDisconnected:
            raise
        except Exception as exc:
            self._raise_if_disconnected()
            # Could not even stat the file - record it and keep going.
            try:
                db.claim_file(path, name, 0, 0, self.job_id)
                db.set_status(path, 'error', error=f'{type(exc).__name__}: {exc}')
            except Exception as record_exc:  # noqa: BLE001
                self.log(f'ERROR could not record the error below in the archive '
                         f'database: {type(record_exc).__name__}: {record_exc}')
            self.log(f'ERROR {path}: {type(exc).__name__}: {exc}')
            completed = True
        finally:
            if completed:
                with self._count_lock:
                    self.processed += 1
                    if stepped_over:
                        self.stepped_over += 1
                    # A count taken from the estimate cannot foresee a file added
                    # since; progress must never read past 100%.
                    if self.processed > self.total_files:
                        self.total_files = self.processed
                self._heartbeat()

    def _copy_everything(self):
        """Walk the sources and archive what is found, `workers` at a time.

        The walk itself stays on this thread and stays ordered, so which of two
        byte-identical photos becomes the archived copy is still decided the
        same way every run. Only the copying overlaps.
        """
        self._copying = True
        try:
            self._copy_walk()
        finally:
            self._copying = False

    def _copy_walk(self):
        if self.workers <= 1:
            for root, name in self._walk():
                self.gate.check()
                self._one_file(os.path.join(root, name), name)
            return

        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        pending = set()
        cancelled = None
        # What the last few workers raised while the queue drained. `_one_file`
        # turns every ordinary failure into a recorded error, so anything that
        # reaches here is the run itself failing — above all DriveDisconnected.
        # Dropping it would let a run whose destination vanished mid-copy go on
        # to report itself complete.
        failure = None
        with ThreadPoolExecutor(max_workers=self.workers,
                                thread_name_prefix='archive') as pool:
            try:
                for root, name in self._walk():
                    self.gate.check()
                    path = os.path.join(root, name)
                    # Keep only a little more work queued than there are
                    # workers: a whole-drive walk can find hundreds of
                    # thousands of files, and queuing them all would hold the
                    # entire list in memory before copying a single one.
                    while len(pending) >= self.workers * 2:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            future.result()
                    pending.add(pool.submit(self._one_file, path, name))
            except Cancelled as stop:
                cancelled = stop
            finally:
                for future in pending:
                    try:
                        future.result()
                    except Cancelled:
                        cancelled = cancelled or Cancelled()
                    except Exception as exc:
                        if failure is None or (isinstance(exc, DriveDisconnected)
                                               and not isinstance(failure, DriveDisconnected)):
                            failure = exc
                # Every worker holds its own SQLite connection; close them so
                # nothing is left owed when the pool is torn down.
                pool.submit(db.flush)
        # A disconnect outranks a cancel: the run loop waits for the drive and
        # resumes, and a user who pressed Stop meanwhile is honoured there.
        if isinstance(failure, DriveDisconnected):
            raise failure
        if cancelled is not None:
            raise cancelled
        if failure is not None:
            raise failure

    # ---------------- verify mode ----------------

    def run_verification(self):
        """
        Phase-8 style audit: re-read every archived file and re-check it against
        the SHA-256 recorded when it was written. This is what makes it safe to
        retire a source drive, and it is the only way to catch bit rot, a bad
        cable, or someone editing a file in the archive by hand.
        """
        rows = db.verified_rows()
        self.total_files = len(rows)
        db.update_job(self.job_id, total_files=self.total_files, phase='verifying')

        missing = mismatched = ok = 0
        for row in rows:
            self.gate.check()
            self.processed += 1
            path, expected = row['destination_path'], row['file_hash']
            if not path or not expected:
                continue
            if not os.path.isfile(long_path(path)):
                self._raise_if_disconnected()
                missing += 1
                db.set_status(row['source_path'], 'error',
                              error='archived file is missing from the destination')
                self.log(f'MISSING {path}')
                continue
            try:
                actual = hash_file(path, self.gate)
            except Cancelled:
                raise
            except OSError as exc:
                missing += 1
                db.set_status(row['source_path'], 'error',
                              error=f'archived file could not be read: {exc}')
                continue
            if actual != expected:
                mismatched += 1
                db.set_status(row['source_path'], 'error', dest_hash=actual,
                              error='audit failed: the archived file no longer '
                                    'matches the hash recorded when it was written')
                self.log(f'MISMATCH {path} expected {expected} got {actual}')
            else:
                ok += 1
                # Most rows an audit reads are already verified with this very
                # hash, and writing that back only forces a commit per file.
                # Only a row that said something else - an earlier audit
                # failure since repaired, or a copy that recorded no hash of
                # the written file - gets the write.
                if row.get('status') != 'verified' or row.get('dest_hash') != actual:
                    db.set_status(row['source_path'], 'verified', dest_hash=actual)

        summary = (f'audit complete - {ok} files matched, {mismatched} changed, '
                   f'{missing} missing')
        self.log(summary)
        db.update_job(self.job_id, state='completed', phase='done',
                      ended_at=time.time(), message=summary)
        self._refresh_status_kit()

    # ---------------- driver ----------------

    def _run_once(self):
        try:
            # The worker owns its own thread and its own connection, so it makes
            # sure the schema exists rather than assuming a caller did it. Without
            # this, a job started outside the web app failed at the first query
            # and the failure had nowhere to be recorded, because recording it
            # needed the very tables that were missing.
            db.init_db()

            # Defence in depth: the correction lives here as well as in
            # start_scan, so the engine can never nest an archive inside itself
            # no matter which entry point started the job.
            self.resolution = resolve_destination(self.destination,
                                                  db.previous_destinations())
            if self.resolution['corrected']:
                self.destination = self.resolution['destination']

            self.job_id = db.create_job(
                [{'path': e['path'],
                  'types': sorted(k for k in MEDIA_KINDS if k in e['types'])}
                 for e in self.source_entries],
                self.destination, 'YYYY/MM/DD', self.mode)
            db.close_orphaned_jobs(self.job_id)
            self._open_log()
            if self.resolution['corrected']:
                self.log('DESTINATION CORRECTED: '
                         f'{self.resolution["original"]} -> {self.destination}')

            if self.mode == MODE_VERIFY:
                self._promote_destination_probe()
                self.run_verification()
                return

            os.makedirs(long_path(self.destination), exist_ok=True)
            self._promote_destination_probe()

            if self.mode == MODE_COPY:
                # A previous dry run's decisions are predictions, not results.
                cleared = db.clear_plan_rows()
                swept = self.sweep_partials()
                # Read what was already settled *after* the predictions are
                # gone and before any of this run's own work lands, so the
                # number describes the moment Start was pressed.
                self.note_prior_progress()
                note = []
                if self.resumed_from:
                    note.append(f'resuming - {self.resumed_from:,} files were '
                                f'already done')
                    self.log(f'RESUMING: {self.resumed_from} files already '
                             f'finished from an earlier run')
                if swept:
                    note.append(f'cleared {swept} incomplete temp files')
                if cleared:
                    note.append(f'discarded {cleared} dry-run predictions')
                db.update_job(self.job_id, phase='enumerating', message='; '.join(note))
            else:
                db.clear_plan_rows()
                db.update_job(self.job_id, phase='enumerating', message='')

            known = recall_estimate(self)
            if known is not None:
                # The estimate just walked these exact sources with these exact
                # settings, so walking them again only to count would be the
                # same minutes spent twice before the first copy.
                self.total_files, self.total_bytes, age = known
                db.update_job(self.job_id, total_files=self.total_files,
                              total_bytes=self.total_bytes)
                self.log(f'COUNT from the estimate {int(age // 60)} min ago: '
                         f'{self.total_files} files, {_human(self.total_bytes)}')
            else:
                self.preflight()
            if self.mode == MODE_COPY:
                self.check_capacity()
                # Stamp the root only once the job is actually going ahead, so a
                # refused job does not leave a marker on a folder it never used.
                # Next time, a destination chosen from somewhere inside this
                # archive is recognised and corrected back to here, instead of
                # nesting a second archive inside the first.
                write_archive_marker(self.destination)

            db.update_job(self.job_id, phase='copying', message='')
            self._copy_everything()

            self._record_unreadable_dirs()

            extras = []
            if self.unreadable_dirs:
                extras.append(f'{len(self.unreadable_dirs)} folders could not be '
                              f'read and were NOT scanned - see the Errors tab')
            for _source, detail in self.device_shortfalls:
                extras.append(f'{detail}; unlock it and press Start again - what '
                              f'was copied is stepped over')
            if self.sidecars_copied:
                extras.append(f'{self.sidecars_copied} sidecar files carried across')
            if self.sidecars_failed:
                extras.append(f'{self.sidecars_failed} sidecar files could NOT be '
                              f'copied - see the log')
            if self.sniffed:
                extras.append(f'{self.sniffed} media files found by content '
                              f'despite an unrecognised extension')
            if self.filtered_out:
                breakdown = ', '.join(
                    f'{self.filtered_by_kind[k]} {k}'
                    for k in MEDIA_KINDS if self.filtered_by_kind.get(k))
                extras.append(f'{self.filtered_out} files not archived because '
                              f'their type was not selected ({breakdown})')
            if self.pruned_destination:
                extras.append(f'the destination archive was skipped where it '
                              f'appeared inside a source ({self.pruned_destination} '
                              f'folders)')
            if self.non_media:
                extras.append(f'{self.non_media} non-media files ignored')
            held = [c for c in self.course_folders if c['state'] == 'held']
            excluded = [c for c in self.course_folders if c['state'] == 'excluded']
            review = [c for c in self.course_folders if c['state'] == 'review']
            if held:
                extras.append(f'{len(held)} folders that look like course material were '
                              f'held back until you decide - review them in the estimate')
            if excluded:
                extras.append(f'{len(excluded)} course-material folders excluded as you chose')
            if review:
                extras.append(f'{len(review)} folders look like course material but hold '
                              f'camera photographs, so they were copied - worth a look')
            counts = {state: sum(e['files'] for e in self.entertainment if e['state'] == state)
                      for state in ('held', 'excluded')}
            if counts['held']:
                extras.append(f'{counts["held"]} files that look like films, TV or music were '
                              f'held back until you decide - review them in the estimate')
            if counts['excluded']:
                extras.append(f'{counts["excluded"]} film, TV and music files excluded as you chose')
            spared = sum(e['spared'] for e in self.entertainment)
            if spared:
                extras.append(f'{spared} files looked like films or music but seem personal '
                              f'(a camera, a voice recording, speech), so they were copied - worth a look')

            if self.mode == MODE_DRY_RUN:
                msg = 'dry run complete - nothing was written'
                if extras:
                    msg += ' (' + '; '.join(extras) + ')'
                msg += '. Press Start to carry out this plan.'
            else:
                # A run that could not list part of the source did not archive
                # what was inside it, and must not lead with the word that says
                # it did. It reads the same in the console and in the run log,
                # and it is the first thing anybody sees about the run.
                incomplete = self.unreadable_dirs or self.device_shortfalls
                msg = 'INCOMPLETE' if incomplete else 'complete'
                if extras:
                    msg += ' - ' + '; '.join(extras)
            self.log(msg)
            db.update_job(self.job_id, state='completed', phase='done',
                          ended_at=time.time(), message=msg)
            if self.mode == MODE_COPY:
                status_kit.remember_course_folders(self.destination,
                                                   self.course_folders + self.entertainment)
                self._refresh_status_kit()
                self._cleanup_staging()

        except DriveDisconnected as outage:
            try:
                self._wait_for_drives(outage)
            except Cancelled:
                db.update_job(self.job_id, state='stopped', phase='stopped',
                              ended_at=time.time(),
                              message='stopped while waiting for a drive')
                self.log('STOPPED while waiting for a drive')
                return False
            return True
        except Cancelled:
            db.update_job(self.job_id, state='stopped', phase='stopped',
                          ended_at=time.time(),
                          message='stopped by user - press Start to resume')
            self.log('STOPPED by user')
        except Exception as exc:
            db.update_job(self.job_id, state='failed', phase='failed',
                          ended_at=time.time(), message=str(exc))
            self.log(f'FAILED {type(exc).__name__}: {exc}')
            if self.job_id is None:
                # Nowhere to record it - the failure happened before the job row
                # existed. Say so out loud rather than exiting silently.
                traceback.print_exc()
        finally:
            self._close_log()
            db.close_db()

    def run(self):
        """Run, automatically restarting after the same drive is reconnected."""
        while True:
            restart = self._run_once()
            if restart is not True:
                return
            # A restarted walk re-counts from zero. Successfully verified rows
            # remain terminal in the database and are stepped over safely.
            self.total_files = 0
            self.total_bytes = 0
            self.processed = 0
            self.stepped_over = 0
            self.finished_in_run = None
            self.started_at = time.time()


def _human(n):
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    v, i = float(n or 0), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f'{v:.1f} {units[i]}' if i else f'{int(v)} B'


# --------------------------------------------------------------------------
# module-level API used by app.py
# --------------------------------------------------------------------------

#: How long a finished estimate stands in for the count a run makes before it
#: copies anything. The run's own walk still decides every file; the estimate
#: only supplies the totals for progress and the up-front space check, so a
#: file added meanwhile is still archived, just not foreseen.
ESTIMATE_REUSE_SECONDS = 30 * 60

_estimate_lock = threading.Lock()
_last_estimate = None          # (signature, finished monotonic, files, bytes)


def _count_signature(job):
    """What a count depends on: sources and kinds, destination, scan, floor, decisions."""
    return json.dumps({
        'sources': [[normalise(e['path']), sorted(e['types'])] for e in job.source_entries],
        'destination': normalise(job.destination),
        'deep_scan': bool(job.deep_scan),
        'min_bytes': MIN_MEDIA_BYTES,
        'decisions': db.folder_decisions_version(),
    }, sort_keys=True)


def remember_estimate(job, files, total_bytes):
    """Keep a completed estimate's totals so Start need not count again."""
    global _last_estimate
    signature = _count_signature(job)
    with _estimate_lock:
        _last_estimate = (signature, time.monotonic(), files, total_bytes)


def recall_estimate(job, max_age=None):
    """(files, bytes, age in seconds) from a recent estimate of this same job, or None."""
    with _estimate_lock:
        known = _last_estimate
    if known is None:
        return None
    signature, finished, files, total_bytes = known
    age = time.monotonic() - finished
    limit = ESTIMATE_REUSE_SECONDS if max_age is None else max_age
    if age > limit or signature != _count_signature(job):
        return None
    return files, total_bytes, age


def forget_estimate():
    global _last_estimate
    with _estimate_lock:
        _last_estimate = None


_job = None
_lock = threading.Lock()


def resolve_archive_destination(destination_dir):
    """Public helper: correct a destination chosen inside an existing archive."""
    try:
        known = db.previous_destinations()
    except Exception:
        known = []
    return resolve_destination(destination_dir, known)


def start_scan(source_dirs, destination_dir, mode=MODE_COPY, media_types=None,
               deep_scan=True, vision=None):
    """
    Returns (ok, problems, resolution). Start is also Resume - state is in the DB.

    `resolution` describes any correction made to the destination, so the caller
    can tell the user their chosen folder was inside an existing archive and the
    archive root was used instead.
    """
    global _job
    with _lock:
        if _job is not None and _job.thread is not None and _job.thread.is_alive():
            return False, ['A job is already running.'], None

        if mode not in (MODE_COPY, MODE_DRY_RUN, MODE_VERIFY):
            return False, [f'Unknown mode: {mode}'], None

        resolution = resolve_archive_destination(destination_dir)
        destination_dir = resolution['destination']

        if mode == MODE_VERIFY:
            if not db.has_verified():
                return False, ['There is nothing to audit yet - run a consolidation '
                               'first.'], resolution
        else:
            problems = validate_job(source_dirs, destination_dir)
            if problems:
                return False, problems, resolution
            # Warn rather than refuse: a job that will archive nothing from a
            # given folder is legal, but almost never intended.
            for entry in normalise_sources(source_dirs, media_types):
                if not entry['types']:
                    return False, [f'"{entry["path"]}" has no media types '
                                   f'selected.'], resolution

        job = ArchiveJob(source_dirs, destination_dir, mode=mode,
                         media_types=media_types, deep_scan=deep_scan, vision=vision)
        job.thread = threading.Thread(target=job.run, name='archive-job', daemon=True)
        _job = job
        job.thread.start()
        return True, [], resolution


def stop_scan():
    if _job is not None:
        _job.gate.cancel()


def pause_scan():
    if _job is not None:
        _job.gate.pause()


def resume_scan():
    if _job is not None:
        _job.gate.resume()


#: Config key set while a person has the job paused. Shutting Ninaivu down also
#: pauses the job (so it stops cleanly), which is why the pause itself cannot
#: say whether anybody chose it.
PAUSED_BY_PERSON = 'paused-by-person'


def remember_pause(paused):
    """Record whether the running job is paused because somebody asked."""
    db.set_config(PAUSED_BY_PERSON, '1' if paused else '')


def resume_interrupted(vision=None):
    """Carry on with a job Ninaivu stopped in the middle of, as Start would.

    Returns ``(job, problems)``: the job row that was picked up, or ``None`` if
    there was nothing to pick up, and why it could not start if it could not —
    a network share that is not back yet looks exactly like that, so the
    caller may try again. Start is resume: finished files are stepped over.

    Only a copy or an audit is picked up. A dry run writes nothing and is
    quicker to start again than to explain. A job somebody had paused comes
    back paused.
    """
    if is_scanning():
        return None, []
    try:
        db.init_db()
        job = db.interrupted_job()
        if job is None or job.get('mode') not in (MODE_COPY, MODE_VERIFY):
            return None, []
        try:
            sources = json.loads(job.get('sources') or '[]')
        except ValueError:
            sources = []
        settings = db.load_settings()
        paused = bool(db.get_config(PAUSED_BY_PERSON))
    finally:
        db.close_db()

    ok, problems, _resolution = start_scan(
        sources, job['destination'], mode=job['mode'],
        deep_scan=settings.get('deep_scan', True), vision=vision)
    if not ok:
        return job, problems
    if paused:
        pause_scan()
    try:
        db.init_db()
        # Closed only once its successor is running, so a start that could not
        # happen yet leaves it to be picked up by the next attempt.
        db.update_job(job['id'], state='interrupted', phase='interrupted',
                      ended_at=time.time(),
                      message='Ninaivu stopped during this run; it carried on '
                              'automatically when Ninaivu started again.')
    finally:
        db.close_db()
    return job, []


def is_scanning():
    return _job is not None and _job.thread is not None and _job.thread.is_alive()


def is_scan_paused():
    return _job is not None and _job.gate.is_paused and is_scanning()


def job_progress():
    """Progress plus a throughput-based ETA, which is the number people want."""
    if _job is None:
        return {'processed': 0, 'stepped_over': 0, 'total_files': 0, 'mode': None,
                'eta_seconds': None, 'finished_in_run': None,
                'resumed_from': 0, 'is_resume': False,
                'waiting_for_drives': [], 'reconnects': 0,
                'pacing': {'mode': 'full-speed', 'reason': ''}}

    processed, total = _job.processed, _job.total_files
    stepped = _job.stepped_over
    eta = None
    if is_scanning() and total and processed:
        elapsed = time.time() - _job.started_at
        # Timed on real work only. Stepping over a finished file takes a
        # database lookup, so a rate that counted those promised the whole
        # rest of a resumed run at lookup speed.
        worked = processed - stepped
        if elapsed > 2 and worked > 0:
            # The files still ahead include whatever finished work the walk has
            # not reached yet; they cost next to nothing.
            ahead = total - processed
            if _job.finished_in_run is not None:
                # Counted among this run's own files, so exact.
                ahead_done = max(0, _job.finished_in_run - stepped)
            else:
                # Counts came from an estimate, so only the archive-wide figure
                # is known. It includes other sources' files and files since
                # deleted or deselected - 171,885 done "of" a 170,050-file run
                # promised 0s with ten thousand files still to go. Once it
                # would swallow everything ahead it is plainly not describing
                # this run, so nothing ahead is assumed done.
                ahead_done = max(0, _job.resumed_from - stepped)
                if ahead_done >= ahead:
                    ahead_done = 0
            remaining = max(0, ahead - ahead_done)
            eta = int(remaining * elapsed / worked)
    return {'processed': processed, 'stepped_over': stepped,
            'total_files': total, 'mode': _job.mode,
            'eta_seconds': eta,
            'finished_in_run': _job.finished_in_run,
            # What was already settled when Start was pressed, so a run picking
            # up after a power cut can say so rather than looking like a fresh
            # one racing through its first three hundred thousand files.
            'resumed_from': _job.resumed_from,
            # Finished files that all belong to other sources are no resume.
            'is_resume': (_job.resumed_from if _job.finished_in_run is None
                          else _job.finished_in_run) > 0,
            'waiting_for_drives': list(_job.waiting_for),
            'reconnects': _job.reconnects,
            'pacing': _job.pacer.snapshot()}
