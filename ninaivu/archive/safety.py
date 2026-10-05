"""
Path safety and job validation.

These checks run before a job is allowed to start. Every one of them exists
because the failure it prevents is silent, expensive, or both.
"""

import json
import os
import re
import sys
import time
import uuid

from ..words import filled, said


class Said(str):
    """A problem or notice in English that also carries the sentence it was
    made from and what was filled into it, so the console can show it in the
    family's language (``i18n.t(key, params)``) while everything else keeps
    reading it as the plain English it is. From Ninaivu Lite's importer."""

    key: str
    params: dict


def _say(template, **params):
    text = Said(filled(template, params))
    text.key = template
    text.params = {name: str(value) for name, value in params.items()}
    return text


def translatable(messages):
    """``{key, params}`` for each message the console can translate, None for
    one it cannot (a reason the operating system gave, for instance): in the
    same order as *messages*, so the two lists are read side by side."""
    return [{'key': m.key, 'params': m.params} if isinstance(m, Said) else None
            for m in messages]

IS_WINDOWS = sys.platform.startswith('win')

# Dropped at the top of an archive so the tool can recognise its own archive
# later, from any folder inside it.
ARCHIVE_MARKER = '.photo-archive-root'
UNDATED_FOLDER = 'Unknown-Date'


def strip_long_prefix(path):
    r"""
    Remove a \\?\ prefix, on every platform.

    Deliberately NOT platform-guarded, unlike short_path. This is the function
    comparisons go through, and a comparison must never depend on which form a
    path happens to be carrying. The walk runs through the long-path form on
    Windows, so a folder inside it arrives as \\?\C:\Root\Master while the
    destination is the plain C:\Root\Master - string-equal they are not, and
    every path check silently answered "no". No real path on any platform
    begins with \\?\, so stripping it unconditionally is safe.
    """
    if not isinstance(path, str):
        return path
    if path.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path[8:]
    if path.startswith('\\\\?\\'):
        return path[4:]
    return path


def normalise(path):
    """Absolute, symlink-resolved, case-folded on Windows. Used for comparisons only."""
    try:
        from ..utils import devices
        if devices and devices.looks_like_device_path(path):
            cleaned = str(path or '').strip().strip('"').replace('/', '\\')
            return os.path.normcase(cleaned)
    except Exception:
        pass
    p = os.path.realpath(os.path.abspath(os.path.expanduser(strip_long_prefix(path))))
    return os.path.normcase(strip_long_prefix(p))


def long_path(path):
    r"""
    Windows silently fails on paths over 260 characters unless LongPathsEnabled
    is set, and the consolidation runbook calls that out as a trap: Get-FileHash
    and ExifTool mark the messiest folders unreadable rather than erroring.
    The \\?\ prefix bypasses the limit regardless of the registry setting.

    No-op on other platforms and on paths that already carry the prefix.
    """
    if not IS_WINDOWS:
        return path
    p = os.path.abspath(path)
    if p.startswith('\\\\?\\'):
        return p
    if p.startswith('\\\\'):                      # UNC \\server\share
        return '\\\\?\\UNC\\' + p[2:]
    return '\\\\?\\' + p


def short_path(path):
    r"""
    Inverse of long_path: strip the \\?\ prefix so paths that get stored,
    logged or shown to the user stay readable. Every file operation re-applies
    long_path, so this is purely cosmetic and never loses reach.
    """
    if not IS_WINDOWS:
        return path
    return strip_long_prefix(path)


def is_within(child, parent):
    """True when `child` is `parent` or lives underneath it."""
    c, p = normalise(child), normalise(parent)
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


# --------------------------------------------------------------------------
# Recognising an archive we already made
# --------------------------------------------------------------------------
#
# The failure this prevents: someone browses into their archive and picks
# MasterArchive\2018 as the destination. Every photo NOT already under 2018 is
# then re-copied into MasterArchive\2018\<year>\..., producing a second archive
# nested inside the first. Worse, the files already under 2018 are left alone,
# so it is a *partial* duplication - much harder to notice than a total one, and
# on a real archive it can be hundreds of gigabytes.

def write_archive_marker(root, layout='YYYY/MM/DD'):
    """Stamp a folder as an archive root. Idempotent; never overwrites an id."""
    path = os.path.join(root, ARCHIVE_MARKER)
    if os.path.isfile(path):
        return read_archive_marker(root)
    data = {'tool': 'photo-archive-manager', 'layout': layout,
            'created': time.time(), 'id': uuid.uuid4().hex}
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        if IS_WINDOWS:
            try:                       # keep it out of the user's way
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)  # HIDDEN
            except Exception:
                pass
    except OSError:
        return None
    return data


def read_archive_marker(root):
    try:
        with open(os.path.join(root, ARCHIVE_MARKER), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _ancestors(path):
    """Yield the path itself, then each parent, up to the filesystem root."""
    current = os.path.abspath(path)
    seen = set()
    while current and current not in seen:
        seen.add(current)
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


_YEAR_RE = re.compile(r'^\d{4}$')
_TWO_DIGIT_RE = re.compile(r'^\d{2}$')


def _is_date_component(name):
    """Does this folder name look like a YYYY / MM / DD level of the layout?"""
    if name == UNDATED_FOLDER:
        return True
    if _YEAR_RE.match(name):
        return 1900 <= int(name) <= 2100
    if _TWO_DIGIT_RE.match(name):
        return 1 <= int(name) <= 31
    return False


def _looks_like_archive_root(path, exclude=None):
    """
    A folder holding year-shaped subfolders, or our marker.

    `exclude` is the child we climbed up through. It must not count as its own
    evidence: `D:\\Backups\\2015` would otherwise make `D:\\Backups` look like an
    archive root purely because `2015` is sitting in it, and a perfectly ordinary
    destination would get silently redirected to its parent.

    The consequence of requiring a sibling is that a real archive containing
    exactly one year cannot be detected this way - but picking that year folder
    duplicates nothing anyway, since there is no other year to re-copy into it.
    """
    if os.path.isfile(os.path.join(path, ARCHIVE_MARKER)):
        return True
    exclude_n = normalise(exclude) if exclude else None
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if exclude_n and normalise(entry.name) == exclude_n:
                    continue
                if entry.name == UNDATED_FOLDER:
                    return True
                if _YEAR_RE.match(entry.name) and 1900 <= int(entry.name) <= 2100:
                    return True
    except OSError:
        pass
    return False


def _climb_out_of_date_folders(path):
    """
    Walk up out of a YYYY\\MM\\DD chain and return the folder above it, but only
    if that folder independently looks like an archive root. Catches archives
    made before markers existed.
    """
    current = os.path.abspath(path)
    child = None
    climbed = False
    while True:
        name = os.path.basename(current.rstrip(os.sep))
        if not name or not _is_date_component(name):
            break
        parent = os.path.dirname(current.rstrip(os.sep))
        if not parent or parent == current:
            break
        current, child, climbed = parent, name, True
    if not climbed:
        return None
    return current if _looks_like_archive_root(current, exclude=child) else None


def find_archive_root(chosen, known_roots=()):
    """
    Work out whether `chosen` sits inside an archive that already exists.

    Returns (root, reason) where reason is 'marker', 'history' or 'layout', or
    (None, None) when `chosen` is a fine destination in its own right - including
    when it IS the root of an existing archive.

    Three signals, most reliable first:
      marker  - one of our own .photo-archive-root files at an ancestor
      history - a destination used by a previous job in this database
      layout  - `chosen` sits inside a YYYY\\MM\\DD chain under a folder that
                looks like an archive (covers archives predating the marker)
    """
    target = os.path.abspath(os.path.expanduser(chosen))
    target_n = normalise(target)

    for ancestor in _ancestors(target):
        if os.path.isfile(os.path.join(ancestor, ARCHIVE_MARKER)):
            if normalise(ancestor) == target_n:
                return None, None            # chosen is itself the root
            return ancestor, 'marker'

    for root in known_roots:
        if not root:
            continue
        root_abs = os.path.abspath(os.path.expanduser(root))
        if normalise(root_abs) == target_n:
            return None, None
        if is_within(target, root_abs):
            return root_abs, 'history'

    root = _climb_out_of_date_folders(target)
    if root and normalise(root) != target_n:
        return root, 'layout'
    return None, None


def resolve_destination(chosen, known_roots=()):
    """
    Return {'destination', 'corrected', 'original', 'reason', 'message'}.

    When the chosen folder turns out to be inside an existing archive, the
    archive's own root is substituted, because that is what the person meant:
    they were browsing their archive and clicked one level too deep.
    """
    chosen = (chosen or '').strip()
    result = {'destination': chosen, 'corrected': False, 'original': chosen,
              'reason': None, 'message': ''}
    if not chosen:
        return result

    root, reason = find_archive_root(chosen, known_roots)
    if not root:
        return result

    explanation = {
        'marker':  'it is inside an archive this tool created',
        'history': 'it is inside the archive folder used by a previous run',
        'layout':  'it is one of the dated folders inside an existing archive',
    }[reason]
    result.update({
        'destination': root,
        'corrected': True,
        'reason': reason,
        'message': (f'"{chosen}" was not used as the destination because '
                    f'{explanation}. Files will be filed from the archive root '
                    f'"{root}" instead. Using the subfolder would have copied '
                    f'the rest of your archive into it a second time.'),
    })
    return result


# --------------------------------------------------------------------------
# Per-source media selection
# --------------------------------------------------------------------------

MEDIA_KINDS = ('image', 'video', 'audio')


def normalise_sources(sources, default_types=None):
    """
    Accept either shape and return [{'path': abspath, 'types': frozenset}, ...].

      ['D:/Photos', ...]                                  - old flat form
      [{'path': 'D:/Photos', 'types': ['image']}, ...]     - per-folder form

    A folder scanned for stills only and a folder scanned for video only are a
    normal thing to want - an old camcorder card has no photos worth sifting,
    and a scanned-print folder has no video. Carrying the selection per source
    rather than globally is what makes that expressible.
    """
    # `default_types is None` means "the caller did not say", which is the
    # legacy flat form and correctly means everything. An explicitly empty or
    # unrecognised selection means what it says and must stay narrow — the
    # alternative is that "archive nothing" silently becomes "archive
    # everything", which is how a photos-only job ends up copying a terabyte
    # of video.
    if default_types is None:
        default = frozenset(MEDIA_KINDS)
    else:
        default = frozenset(default_types) & frozenset(MEDIA_KINDS)

    out = []
    for entry in sources or []:
        if isinstance(entry, dict):
            path = (entry.get('path') or '').strip()
            if 'types' in entry:
                types = frozenset(entry.get('types') or ()) & frozenset(MEDIA_KINDS)
            else:
                types = default
        else:
            path = str(entry).strip()
            types = default
        if not path:
            continue
        try:
            from ..utils import devices
            is_device = devices and devices.looks_like_device_path(path)
        except Exception:
            is_device = False
        if is_device:
            norm_p = str(path).strip().strip('"').replace('/', '\\')
            out.append({'path': norm_p, 'types': types})
        else:
            out.append({'path': os.path.abspath(os.path.expanduser(path)),
                        'types': types})
    return out


def source_paths(sources):
    """Just the folder paths, in order, from either shape."""
    return [s['path'] for s in normalise_sources(sources)]


def describe_types(types):
    labels = {'image': 'photos', 'video': 'video', 'audio': 'audio'}
    chosen = [labels[k] for k in MEDIA_KINDS if k in types]
    if not chosen:
        return 'nothing'
    if len(chosen) == len(MEDIA_KINDS):
        return 'photos, video and audio'
    return ' and '.join([', '.join(chosen[:-1]), chosen[-1]] if len(chosen) > 1
                        else chosen)



def _shell_hint(path):
    """A phone or camera shown by Windows is not a folder anything can open."""
    try:
        from ..server.config import shell_namespace_hint            # noqa: PLC0415

        hint = shell_namespace_hint(path)
    except Exception:                                        # noqa: BLE001
        return None
    if not hint:
        return None
    return (hint + " Copy what you want off it first — Explorer, or Windows' "
                   "own Import — and give Ninaivu that folder.")


def _is_network(path):
    text = str(path or '')
    if text.startswith('\\\\') or text.startswith('//'):
        return True
    try:
        from ..server.config import network_drives                  # noqa: PLC0415

        drive = os.path.splitdrive(os.path.abspath(text))[0]
        return bool(drive) and f'{drive}\\' in network_drives()
    except Exception:                                        # noqa: BLE001
        return False


def _why_not_a_folder(path, role):
    """Say what is actually wrong, rather than that something is missing.

    "Does not exist" is the same sentence for a mistyped path, an unplugged
    drive, a phone that was never a folder, and a share that is there but will
    not let this account in. Only the first of those is helped by checking
    the spelling.
    """
    hint = _shell_hint(path)
    if hint:
        return hint
    if _is_network(path):
        return _say(
            said("Folder “{path}” is a network location that did not answer. Either the share is offline, or Ninaivu is running as an account that has not signed in to it. Windows keeps network sign-ins per user, so a service, or a different user, sees none of yours. Open it once in Explorer as the account Ninaivu runs under, or map it for that account."),
            path=path)
    # Only sources come here; *role* is kept for the callers' sake.
    if os.path.exists(path):
        return _say(said("Source path exists but is not a folder: {path}"), path=path)
    return _say(said("Source folder does not exist or is not a folder: {path}"), path=path)


def _relative_paths(sources):
    """The sources given without a full path (a phone's path aside)."""
    out = []
    for entry in sources or []:
        path = str((entry.get('path') if isinstance(entry, dict) else entry) or '').strip()
        if not path:
            continue
        try:
            from ..utils import devices                              # noqa: PLC0415
            if devices.looks_like_device_path(path):
                continue
        except Exception:                                    # noqa: BLE001
            pass
        if not os.path.isabs(os.path.expanduser(path)):
            out.append(path)
    return out


def validate_job(sources, destination, protected=()):
    """
    Return a list of human-readable problems. An empty list means the job is
    safe to start. Accepts either source shape.

    *protected* are folders the archive must never be written into: Ninaivu's
    own data folder, whose index, thumbnails and backups would be mixed with
    the family's photographs and lost with them on a reset (from Ninaivu Lite).
    """
    problems = []

    entries = normalise_sources(sources)
    if not entries:
        problems.append(_say(said("Add at least one source folder.")))
    if not destination:
        problems.append(_say(said("Choose a destination folder for the archive.")))
    if problems:
        return problems

    # A folder with nothing ticked would be walked and then discard everything
    # it found, which looks like a broken scan rather than a deliberate choice.
    for entry in entries:
        if not entry['types']:
            problems.append(_say(
                said("No media types are selected for source “{path}”. Tick at least one of photos, video or audio, or remove the folder."),
                path=entry['path']))

    # A path typed without its drive or folder would be read from wherever
    # Ninaivu happens to have started, which is nowhere the person meant
    # (from Ninaivu Lite).
    for path in _relative_paths(sources):
        problems.append(_say(said("Give the full path of the source folder, not “{path}”."),
                             path=path))
        entries = [e for e in entries
                   if e['path'] != os.path.abspath(os.path.expanduser(path))]

    if entries and not any(e['types'] for e in entries):
        problems.append(_say(
            said("Nothing would be archived: every source has all media types switched off.")))

    sources = [e['path'] for e in entries]

    try:
        from ..utils import devices
    except Exception:
        devices = None

    for s in sources:
        if devices and devices.looks_like_device_path(s):
            reason = devices.unavailable_reason()
            if reason:
                problems.append(_say(said("Source “{path}” is a phone or camera: {reason}"),
                                     path=s, reason=reason))
                continue
            try:
                exists = getattr(devices, 'folder_exists', lambda p: bool(devices.list_folder(p)))(s)
                if not exists:
                    problems.append(_say(
                        said("“{path}” is not on the device any more. If the phone has locked itself, unlock it and try again. Windows hides the storage until you do."),
                        path=s))
            except Exception as exc:
                problems.append(_say(said("Source “{path}” on the device could not be read: {reason}"),
                                     path=s, reason=exc))
            continue
        if os.path.isdir(s):
            continue
        problems.append(_why_not_a_folder(s, 'Source'))

    # The destination must exist or be creatable.
    hint = _shell_hint(destination)
    if hint:
        problems.append(hint)
    elif not os.path.isabs(os.path.expanduser(destination)):
        problems.append(_say(
            said("Give the full path of the destination folder, for example {example}."),
            example=os.path.join('D:\\', 'Photo Archive') if os.name == 'nt'
            else os.path.join(os.path.expanduser('~'), 'Photo Archive')))
    elif os.path.exists(destination) and not os.path.isdir(destination):
        problems.append(_say(said("Destination exists but is not a folder: {path}"),
                             path=destination))
    for folder in protected:
        if folder and (normalise(str(folder)) == normalise(destination)
                       or is_within(destination, str(folder))):
            problems.append(_say(
                said("Destination “{path}” is inside Ninaivu's own data folder “{folder}”. Choose a folder of your own for the archive."),
                path=destination, folder=folder))

    for s in sources:
        if not os.path.isdir(s):
            continue
        if normalise(s) == normalise(destination):
            problems.append(_say(
                said("Destination is the same folder as source “{path}”. The archive must be somewhere else entirely."),
                path=s))
        elif is_within(s, destination):
            problems.append(_say(
                said("Source “{path}” is inside the destination “{destination}”, so the whole of it would be skipped as part of the archive and nothing would be scanned. Choose a source outside the archive."),
                path=s, destination=destination))
        # A destination INSIDE a source is fine and common - an archive on the
        # same drive you are scanning. The walk steps around the destination
        # instead, so it is a notice rather than a refusal (see job_notices).

    # Duplicate / nested sources produce double work and confusing dedup results.
    seen = {}
    for s in sources:
        n = normalise(s)
        if n in seen:
            problems.append(_say(said("Source listed twice: {path}"), path=s))
        seen[n] = s
    for a in sources:
        for b in sources:
            if a is not b and os.path.isdir(a) and os.path.isdir(b) \
                    and normalise(a) != normalise(b) and is_within(a, b):
                problems.append(_say(said("Source “{path}” is already covered by source “{other}”."),
                                     path=a, other=b))

    return problems


def job_notices(sources, destination, libraries=()):
    """
    Things the user should know about a job that is nevertheless safe to run.

    Distinct from validate_job's problems: these do not block anything. The
    main one is a destination sitting inside a source, which used to be refused
    outright and is now handled by stepping around the archive during the walk -
    that is what makes "scan the whole of C: into C:\\Master" work.
    """
    notices = []
    if not destination:
        return notices
    for entry in normalise_sources(sources):
        s = entry['path']
        if not os.path.isdir(s):
            continue
        if not entry['types']:
            # Not an error — a job can legitimately be narrowed folder by
            # folder — but a source set to scan for nothing gets walked in full
            # and archives not one file, which reads as a failure.
            notices.append(_say(
                said("Source “{path}” has no kinds of file selected, so nothing in it will be archived. Choose photos, video or audio on that row."),
                path=s))
            continue
        if normalise(s) == normalise(destination):
            continue                       # already a hard error
        if is_within(destination, s):
            notices.append(_say(
                said("The archive “{destination}” sits inside source “{path}”. It will be skipped during the scan, so files already archived are not read back in as new sources. Everything else under that source is still scanned."),
                destination=destination, path=s))
        # From Ninaivu Lite, where it is a refusal. Here it is a notice:
        # archiving the library into a tidy archive and then swapping one
        # for the other is a real migration. But adding the archive while
        # the old folder is still a library shows every photograph twice.
        for library in libraries:
            if library and (normalise(s) == normalise(str(library))
                             or is_within(s, str(library))):
                notices.append(_say(
                    said("Source “{path}” is already in the library (“{library}”). If you add the archive to the library as well, those photos will show twice until the old folder is removed."),
                    path=s, library=library))
                break
    return notices


def check_free_space(destination, required_bytes, headroom=1.10):
    """
    Return (ok, free_bytes, needed_bytes). Running a consolidation onto a volume
    that fills up halfway is the single most common way these jobs die.
    """
    probe = destination
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        free = os.statvfs(probe).f_frsize * os.statvfs(probe).f_bavail \
            if hasattr(os, 'statvfs') else __import__('shutil').disk_usage(probe).free
    except Exception:
        return True, 0, 0                 # can't tell; don't block the user
    needed = int(required_bytes * headroom)
    return free >= needed, free, needed
