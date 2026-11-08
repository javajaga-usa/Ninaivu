"""How the computers stand, the ways from one standing to another, and the
proof that nothing was lost on the way.

Going from two one-computer Ninaivus to a pair is a sequence the household
carries out by hand, in order: pair, Look on the Secondary, Bring them in,
keep the library as the originals, tick the family view. Each step keeps
every file (mugil/hold.py, survey.py, bring_in.py, adopt.py), but nothing
said afterwards that every file seen before still exists somewhere. On the
household's own computers that is the whole question: the Linux PC's N300
holds the library, and files imported only there, and none of it may be lost.

This adds three things around those steps, and changes none of them:

**Standings and transitions.** A computer is ``single``, ``primary`` or
``secondary`` (``mugil_role``), paired or not. The ways between them are
named (:data:`TRANSITIONS`): ``pair`` (two singles become a pair),
``move_primary`` (a whole-index export carries the pairing to another
computer), ``replace_secondary_disk`` and ``unpair``. :func:`why_not` says
whether a transition can start from where a computer stands, and
:func:`after` where it leaves it.

**The census.** Every file this computer keeps, in its libraries and, on a
Secondary, in its originals folder: where it is, its size and its fingerprint
(storage/fingerprints.py, the kind the two computers already compare). It is
a gzip JSON-lines file in the state folder, named by when it was taken: a
header line, a line per file, and an end line, so a census cut short is never
taken for a whole one. A fingerprint the index, the record of the originals
or the last census already has, for a file of the same size and modified
time, is used as it is; everything else is read end to end. The Primary asks
the Secondary for its census over the signed peer API.

**The plan and the proof.** For ``pair``, the plan is a dry run over the two
censuses taken before: per file, what the pairing and bringing in will do
with it, by the rules the survey and the bring-in already follow
(:func:`plan_pair`). After the household has carried the change out, the
proof takes both censuses again and checks that every fingerprint present on
either computer before is present on at least one of them after
(:func:`prove`). The change is marked done only when the proof passes, and a
proof that fails names each missing file by its path.

Nothing here moves, changes or removes a file; it only reads, and writes its
own files in the state folder (``mugil-census-*``, ``mugil-topology.json``,
``mugil-plan-*``, ``mugil-missing-*``). The record is a JSON file rather than a
table, so a 3.x index opens with nothing to do.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from . import PRIMARY, SECONDARY, SINGLE, pairing, role
from .bring_in import _besides, _deleted_here
from .courier import AWAY, TIMEOUT, Partner, PartnerError
from .hold import DISK_ID, KNOWN_DISKS, LEDGER, SET_ASIDE
from .survey import _INBOX, _RECYCLE, _junk
from ..storage import fingerprints
from ..utils.files import atomic_write

log = logging.getLogger(__name__)

# -- standings and transitions -------------------------------------------------

#: Two singles become a Primary and a Secondary, paired.
PAIR = "pair"
#: The Primary's whole index, pairing and all, goes to another computer.
MOVE_PRIMARY = "move_primary"
#: The Secondary keeps its originals on another disk from now on.
REPLACE_SECONDARY_DISK = "replace_secondary_disk"
#: The two stop being a pair; each keeps its part until it is changed.
UNPAIR = "unpair"
TRANSITIONS = (PAIR, MOVE_PRIMARY, REPLACE_SECONDARY_DISK, UNPAIR)


@dataclass(frozen=True)
class Standing:
    """Where one computer stands: its part, and whether it is paired."""

    role: str
    paired: bool


#: Where each transition may start from, on the computer that takes part.
STARTS: dict[str, frozenset[Standing]] = {
    # A part may be chosen before the pairing (the console asks for it first),
    # so an unpaired Primary or Secondary is still on its way from single.
    PAIR: frozenset({Standing(SINGLE, False), Standing(PRIMARY, False),
                     Standing(SECONDARY, False)}),
    # The Primary it leaves, and the one-computer Ninaivu the index goes to.
    MOVE_PRIMARY: frozenset({Standing(PRIMARY, True), Standing(SINGLE, False)}),
    # Both of the pair take part: the Secondary's disk, the Primary's resend.
    REPLACE_SECONDARY_DISK: frozenset({Standing(PRIMARY, True), Standing(SECONDARY, True)}),
    UNPAIR: frozenset({Standing(PRIMARY, True), Standing(SECONDARY, True)}),
}

#: Why a transition cannot start where a computer stands, as a "reason" so
#: tools/server_messages.py collects it for translation.
NOT_FROM_HERE = {
    PAIR: {"reason": "Only a computer that is not paired can be paired."},
    MOVE_PRIMARY: {"reason": "Only the paired Primary, or a one-computer Ninaivu taking its index, "
                             "takes part in moving the Primary."},
    REPLACE_SECONDARY_DISK: {"reason": "Only a paired Primary or Secondary takes part in "
                                       "replacing the Secondary's disk."},
    UNPAIR: {"reason": "Only a paired computer can be unpaired."},
}
NO_SUCH = {"reason": "There is no such change."}
WHICH_PART = {"reason": "Say which part this computer takes: primary or secondary."}
PART_CHOSEN = {"reason": "This computer's part is chosen already: it pairs as that part."}


def standing(cfg) -> Standing:
    """Where *cfg*'s computer stands now."""
    return Standing(role(cfg), pairing.load(cfg.state_dir) is not None)


def why_not(transition: str, here: Standing, part: str | None = None) -> str | None:
    """Why *transition* cannot start from *here*, or None when it can.

    *part* is the part this computer takes in a pairing; one whose part is
    chosen already takes that one.
    """
    if transition not in STARTS:
        return NO_SUCH["reason"]
    if here not in STARTS[transition]:
        return NOT_FROM_HERE[transition]["reason"]
    if transition == PAIR:
        wanted = part or (here.role if here.role != SINGLE else None)
        if wanted not in (PRIMARY, SECONDARY):
            return WHICH_PART["reason"]
        if here.role != SINGLE and wanted != here.role:
            return PART_CHOSEN["reason"]
    return None


def after(transition: str, here: Standing, part: str | None = None) -> Standing:
    """Where *transition* leaves a computer that started it from *here*.

    Raises ValueError when it cannot start there (:func:`why_not`).
    """
    problem = why_not(transition, here, part)
    if problem:
        raise ValueError(problem)
    if transition == PAIR:
        return Standing(part or here.role, True)
    if transition == MOVE_PRIMARY:
        # The computer the index left is unpaired and goes back to being one
        # computer; the one it went to is the Primary of the same pair.
        return Standing(SINGLE, False) if here.role == PRIMARY else Standing(PRIMARY, True)
    if transition == UNPAIR:
        return Standing(here.role, False)
    return here                                           # replace_secondary_disk


# -- the census ----------------------------------------------------------------

#: The census files this computer takes, in its state folder.
CENSUS_PREFIX = "mugil-census-"
#: A copy of the other computer's census, fetched for a plan or a proof.
PARTNER_PREFIX = "mugil-partner-"
_CENSUS_NAME = re.compile(r"^mugil-census-\d{8}-\d{6}(-\d+)?\.jsonl\.gz$")
#: What each census taken here says about itself, for listing them quickly.
CENSUS_LIST = "mugil-censuses.json"
#: The kind of census file this writes.
FORMAT = 1
#: Own censuses kept besides the ones a transition still needs.
KEEP_CENSUSES = 3

#: Where in a computer a file was found: a library, its recycle bin, the
#: Secondary's originals folder and what is beside it there.
LIBRARY, RECYCLE, ORIGINALS, SET_ASIDE_AREA, INBOX, STRAY = (
    "library", "recycle", "originals", "set_aside", "inbox", "stray")


class Stopped(Exception):
    """A census asked to stop part-way: nothing is written."""


def _who(cfg) -> str:
    try:
        from . import display_name                                 # noqa: PLC0415
        return display_name(cfg)
    except Exception:                                              # noqa: BLE001 - a name only
        return str(getattr(cfg, "house_name", "") or "this computer")


def _ignored(cfg) -> set[str]:
    """Folders the scan steps over, but not the recycle bin: what is in it
    is still the household's, and the plan says it is left alone."""
    return set(getattr(cfg, "ignore_dirs", None) or ()) - {_RECYCLE}


def _originals_away(folder: Path, state_dir: Path) -> bool:
    """Whether the disk the originals are kept on is away: not there, or an
    empty folder where a disk with an id was (as hold.disk_problem sees it,
    without writing anything)."""
    if not folder.is_dir():
        return True
    if (folder / DISK_ID).exists():
        return False
    try:
        known = json.loads((state_dir / KNOWN_DISKS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(known, dict) and bool(known.get(str(folder)))


def _tops(cfg) -> list[dict[str, Any]]:
    """Each folder a census walks: the libraries, then the originals folder."""
    from ..storage import roots as roots_kit                       # noqa: PLC0415
    tops: list[dict[str, Any]] = []
    for root in list(getattr(cfg, "roots", None) or []):
        tops.append({"top": str(root), "kind": LIBRARY,
                     "away": not (os.path.isdir(root) and roots_kit.available(root))})
    folder = str(getattr(cfg, "mugil_originals_dir", "") or "")
    if role(cfg) == SECONDARY and folder:
        place = Path(folder).expanduser()
        tops.append({"top": str(place), "kind": ORIGINALS,
                     "away": _originals_away(place, Path(cfg.state_dir))})
    return tops


def _files(folder: Path, ignore: set[str]) -> Iterator[tuple[str, Path]]:
    """``(path from folder, Path)`` of every plain file under *folder*, as the
    survey walks one: no links, none of the system's or Ninaivu's own."""
    for here, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not _junk(d) and d not in ignore
                         and not os.path.islink(os.path.join(here, d)))
        for name in sorted(names):
            if not _junk(name):
                full = Path(here) / name
                yield full.relative_to(folder).as_posix(), full


def _walk_top(top: dict[str, Any], ignore: set[str]) -> Iterator[tuple[str, str, Path]]:
    """``(area, path from the top, Path)`` of every file in one top folder."""
    base = Path(top["top"])
    if top["kind"] == LIBRARY:
        for rel, full in _files(base, ignore):
            yield (RECYCLE if rel.split("/", 1)[0] == _RECYCLE else LIBRARY), rel, full
        return
    for entry in sorted(os.listdir(base)):
        here = base / entry
        if entry.startswith(LEDGER) or entry == DISK_ID or here.is_symlink():
            continue
        if entry == SET_ASIDE:
            for rel, full in _files(here, set()):
                yield SET_ASIDE_AREA, f"{entry}/{rel}", full
        elif entry == _INBOX:
            for rel, full in _files(here, set()):
                yield INBOX, f"{entry}/{rel}", full
        elif _junk(entry):
            continue
        elif here.is_file():
            yield STRAY, entry, here
        elif here.is_dir() and entry not in ignore:
            for rel, full in _files(here, ignore):
                area = RECYCLE if rel.split("/", 1)[0] == _RECYCLE else ORIGINALS
                yield area, f"{entry}/{rel}", full


class _Known:
    """Fingerprints already taken, by the file's whole path: from the last
    census, the index and the record of the originals. Kept in a private
    SQLite file, so a disk of millions is not held in memory."""

    def __init__(self) -> None:
        self.db = sqlite3.connect("")
        self.db.executescript(
            "CREATE TABLE known (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, digest TEXT);"
            "CREATE TABLE indexed (path TEXT PRIMARY KEY);"
            "CREATE TABLE replaced (path TEXT PRIMARY KEY);")

    def add(self, rows) -> None:
        self.db.executemany("INSERT OR REPLACE INTO known VALUES (?,?,?,?)",
                            ((os.path.normpath(p), int(s), float(m), d) for p, s, m, a, d in rows
                             if a == fingerprints.ALGO and d))

    def mark(self, table: str, paths) -> None:
        self.db.executemany(f"INSERT OR IGNORE INTO {table} VALUES (?)",
                            ((os.path.normpath(p),) for p in paths))

    def digest(self, path: str, size: int, mtime: float) -> str | None:
        row = self.db.execute("SELECT size, mtime, digest FROM known WHERE path = ?",
                              (os.path.normpath(path),)).fetchone()
        if row and row[0] == size and abs(row[1] - mtime) <= 0.001:
            return row[2]
        return None

    def has(self, table: str, path: str) -> bool:
        return self.db.execute(f"SELECT 1 FROM {table} WHERE path = ?",
                               (os.path.normpath(path),)).fetchone() is not None

    def from_census(self, path: Path) -> None:
        header = read_header(path)
        tops = [t["top"] for t in header.get("tops") or []]
        batch = []
        for entry in entries(path):
            if entry.get("d"):
                batch.append((os.path.join(tops[entry["t"]], entry["p"]), entry["s"], entry["m"],
                              header.get("algo"), entry["d"]))
            if len(batch) >= 2000:
                self.add(batch)
                batch = []
        self.add(batch)

    def from_index(self, conn: sqlite3.Connection, roots: list[str]) -> None:
        for root in roots:
            rows = conn.execute(
                "SELECT a.rel_path, a.trashed, f.algo, f.digest, f.size, f.mtime FROM assets a "
                "LEFT JOIN content_fingerprints f ON f.asset_id = a.id WHERE a.root = ?",
                (str(root),)).fetchall()
            self.mark("indexed", (os.path.join(root, r[0]) for r in rows if not r[1]))
            self.add((os.path.join(root, r[0]), r[4], r[5], r[2], r[3]) for r in rows if r[3])

    def from_ledger(self, folder: Path) -> None:
        ledger = folder / LEDGER
        if not ledger.is_file():
            return
        conn = sqlite3.connect(f"{ledger.as_uri()}?mode=ro", uri=True)
        try:
            self.add((os.path.join(folder, r[0], r[1]), r[2], r[3], r[4], r[5]) for r in conn.execute(
                "SELECT l.folder, k.rel_path, k.size, k.mtime, k.algo, k.digest FROM kept k "
                "JOIN libraries l ON l.id = k.library"))
            self.add((os.path.join(folder, r[0]), r[1], r[2], r[3], r[4]) for r in conn.execute(
                "SELECT path, size, mtime, algo, digest FROM found"))
            asides = conn.execute("SELECT aside, size, mtime, algo, digest FROM set_aside").fetchall()
            self.add((os.path.join(folder, r[0]), r[1], r[2], r[3], r[4]) for r in asides)
            # What the Primary itself set aside: its own older version.
            self.mark("replaced", (os.path.join(folder, r[0]) for r in asides))
        except sqlite3.Error as exc:
            log.info("The record of the originals could not be read for the census: %s", exc)
        finally:
            conn.close()


def _new_name(state_dir: Path, when: float) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    name, n = f"{CENSUS_PREFIX}{stamp}.jsonl.gz", 1
    while (state_dir / name).exists():
        n += 1
        name = f"{CENSUS_PREFIX}{stamp}-{n}.jsonl.gz"
    return name


def take_census(cfg, *, connect_db: Callable[[], sqlite3.Connection] | None = None,
                stop: Callable[[], bool] | None = None,
                progress: Callable[[int, int], Any] | None = None,
                read_all: bool = False) -> Path:
    """List every file this computer keeps, with its fingerprint; the file.

    Each line after the header is ``{"t": top, "p": path from the top, "a":
    area, "s": size, "m": modified time, "d": fingerprint}``, with ``"i": 1``
    for a library file the index knows and ``"r": 1`` for a file the Primary
    itself set aside. A file that could not be read whole has no fingerprint
    and is counted as unreadable. *read_all* reads every file again rather
    than trusting a fingerprint already taken.
    """
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    taken = time.time()
    tops = _tops(cfg)
    here = standing(cfg)
    known = _Known()
    if not read_all:
        last = newest(state_dir)
        if last is not None:
            try:
                known.from_census(state_dir / last["file"])
            except (OSError, ValueError, EOFError):
                pass
    conn = connect_db() if connect_db is not None else None
    if conn is not None:
        try:
            # The index tells which files it knows even when every byte is read.
            if read_all:
                fresh = _Known()
                fresh.from_index(conn, [t["top"] for t in tops if t["kind"] == LIBRARY])
                known.mark("indexed", (r[0] for r in fresh.db.execute("SELECT path FROM indexed")))
            else:
                known.from_index(conn, [t["top"] for t in tops if t["kind"] == LIBRARY])
        except sqlite3.Error as exc:
            log.info("The index could not be read for the census: %s", exc)
    for top in tops:
        if top["kind"] == ORIGINALS and not top["away"]:
            if read_all:
                fresh = _Known()
                fresh.from_ledger(Path(top["top"]))
                known.mark("replaced", (r[0] for r in fresh.db.execute("SELECT path FROM replaced")))
            else:
                known.from_ledger(Path(top["top"]))
    header = {"census": FORMAT, "name": _who(cfg), "role": here.role, "paired": here.paired,
              "taken_at": taken, "algo": fingerprints.ALGO, "tops": tops}
    name = _new_name(state_dir, taken)
    partial = state_dir / f".{name}.partial-{secrets.token_hex(4)}"
    files = size_total = unreadable = 0
    ignore = _ignored(cfg)
    try:
        with gzip.open(partial, "wt", encoding="utf-8") as out:
            out.write(json.dumps(header) + "\n")
            for index, top in enumerate(tops):
                if top["away"]:
                    continue
                for area, rel, full in _walk_top(top, ignore):
                    if stop is not None and stop():
                        raise Stopped()
                    try:
                        st = full.lstat()
                    except OSError:
                        continue
                    whole = str(full)
                    digest = None if read_all else known.digest(whole, st.st_size, st.st_mtime)
                    size, mtime = int(st.st_size), float(st.st_mtime)
                    if digest is None:
                        try:
                            found = fingerprints.fingerprint_file(full, stop)
                        except (OSError, fingerprints.Unchanged):
                            found = ()
                        if found is None:
                            raise Stopped()
                        if found:
                            digest, size, mtime = found
                        else:
                            unreadable += 1
                    entry: dict[str, Any] = {"t": index, "p": rel, "a": area, "s": size,
                                             "m": mtime, "d": digest}
                    if area == LIBRARY and known.has("indexed", whole):
                        entry["i"] = 1
                    if area == SET_ASIDE_AREA and known.has("replaced", whole):
                        entry["r"] = 1
                    out.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    files += 1
                    size_total += size
                    if progress is not None and files % 200 == 0:
                        progress(files, size_total)
            out.write(json.dumps({"end": True, "files": files, "bytes": size_total,
                                  "unreadable": unreadable, "finished": time.time()}) + "\n")
        os.replace(partial, state_dir / name)
    finally:
        partial.unlink(missing_ok=True)
        known.db.close()
    summary = {"file": name, "name": header["name"], "role": here.role, "paired": here.paired,
               "taken_at": taken, "files": files, "bytes": size_total, "unreadable": unreadable,
               "away": [t["top"] for t in tops if t["away"]]}
    _remember(state_dir, summary)
    log.info("Census %s: %s files, %s unreadable", name, files, unreadable)
    return state_dir / name


def read_header(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        header = json.loads(handle.readline() or "null")
    if not isinstance(header, dict) or header.get("census") != FORMAT:
        raise ValueError("That is not a census.")
    return header


def entries(path: Path) -> Iterator[dict[str, Any]]:
    """Every file line of the census at *path*."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        handle.readline()
        for line in handle:
            entry = json.loads(line)
            if entry.get("end"):
                return
            yield entry


def check_census(path: Path) -> dict[str, Any]:
    """The header of a whole census, with its end line's counts; ValueError
    for a file that is not one or was cut short."""
    try:
        header = read_header(path)
        end: dict[str, Any] = {}
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                end = json.loads(line)
    except (OSError, EOFError, ValueError) as exc:
        raise ValueError("The census could not be read whole.") from exc
    if not end.get("end"):
        raise ValueError("The census was cut short.")
    return {**header, **{k: end[k] for k in ("files", "bytes", "unreadable", "finished")}}


def _load_list(state_dir: Path) -> list[dict[str, Any]]:
    try:
        listed = json.loads((state_dir / CENSUS_LIST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in listed if isinstance(c, dict) and (state_dir / str(c.get("file"))).is_file()] \
        if isinstance(listed, list) else []


def _remember(state_dir: Path, summary: dict[str, Any]) -> None:
    listed = [c for c in _load_list(state_dir) if c.get("file") != summary["file"]]
    listed.append(summary)
    listed.sort(key=lambda c: float(c.get("taken_at") or 0))
    atomic_write(state_dir / CENSUS_LIST, json.dumps(listed, indent=2))


def censuses(state_dir: Path | str) -> list[dict[str, Any]]:
    """The censuses taken here, oldest first."""
    return _load_list(Path(state_dir))


def newest(state_dir: Path | str, allowed: Callable[[Standing], bool] | None = None
           ) -> dict[str, Any] | None:
    """The newest census taken here, of those taken where *allowed* says."""
    for census in reversed(censuses(state_dir)):
        if allowed is None or allowed(Standing(str(census.get("role")), bool(census.get("paired")))):
            return census
    return None


def prune(state_dir: Path | str, keep: set[str]) -> None:
    """Remove censuses nothing needs: all but the newest few taken here and
    those a transition still compares against (*keep*)."""
    state_dir = Path(state_dir)
    listed = censuses(state_dir)
    newest_few = {c["file"] for c in listed[-KEEP_CENSUSES:]}
    for census in listed:
        if census["file"] not in newest_few and census["file"] not in keep:
            (state_dir / census["file"]).unlink(missing_ok=True)
    for path in state_dir.glob(f"{PARTNER_PREFIX}*.jsonl.gz"):
        if path.name not in keep:
            path.unlink(missing_ok=True)
    atomic_write(state_dir / CENSUS_LIST, json.dumps(
        [c for c in listed if (state_dir / c["file"]).is_file()], indent=2))


def _place(header: dict[str, Any], entry: dict[str, Any]) -> tuple[str, str]:
    """``(library folder name, path in that library)`` of a census line: the
    same for a file in the library itself and for its copy in the
    Secondary's originals folder, which is named after the library."""
    top = header["tops"][entry["t"]]
    path = unicodedata.normalize("NFC", entry["p"])
    if top["kind"] == LIBRARY:
        return PurePosixPath(top["top"].replace("\\", "/")).name, path
    if entry["a"] in (INBOX, STRAY):
        return "", path
    parts = path.split("/")
    if entry["a"] == SET_ASIDE_AREA:                     # .ninaivu-set-aside/<when>/<library>/<path>
        return (parts[2], "/".join(parts[3:])) if len(parts) > 3 else ("", path)
    return parts[0], "/".join(parts[1:])


def _where(header: dict[str, Any], entry: dict[str, Any]) -> str:
    return os.path.join(header["tops"][entry["t"]]["top"], entry["p"])


# -- the plan --------------------------------------------------------------------

#: What happens to a file in a pairing.
BOTH, BROUGHT_IN, BESIDE, SENT, LEFT_ALONE = "both", "brought_in", "beside", "sent", "left_alone"
OUTCOMES = (BOTH, BROUGHT_IN, BESIDE, SENT, LEFT_ALONE)

#: Why a file is left alone, as the plan says it.
REASONS: dict[str, dict[str, str]] = {
    "recycle_bin": {"reason": "In a recycle bin: kept where it is, not brought in."},
    "deleted_on_primary": {"reason": "Somebody deleted it on the Primary: kept on the "
                                     "Secondary, not brought back."},
    "outside_library": {"reason": "In a folder that is no library of the Primary's: kept where "
                                  "it is. Move it into a library folder and look again."},
    "not_in_index": {"reason": "On the Primary but not in its index, so it is not sent: it "
                               "stays on the Primary."},
    "replaced_by_primary": {"reason": "An older version the Primary itself replaced: kept set "
                                      "aside on the Secondary."},
    "phone_inbox": {"reason": "Waiting in the phone inbox: the Primary's next run files it."},
}


def plan_pair(primary: Path, secondary: Path, out: Path, *,
              conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """What pairing does with every file of the two censuses; the totals.

    The rules are the survey's and the bring-in's (mugil/survey.py,
    mugil/bring_in.py), applied to the censuses instead of to the disks:

    * a Secondary file in a folder that is no library of the Primary's, in a
      recycle bin, or that the Primary set aside itself, is left alone;
    * one whose content the Primary has, at that path or (in its index) at
      another, is already on both;
    * one somebody deleted or erased on the Primary is left alone;
    * one at a path where the Primary has different content is kept beside
      it, as ``<name> (from <Secondary>).<ext>``, its sidecar with it;
    * anything else is brought into the Primary at the same path;
    * a Primary file the Secondary has is on both; one its index knows is
      sent to the Secondary; one in its recycle bin or not in its index
      stays on the Primary.

    One line per file goes to *out* (gzip JSON lines); *conn* is the
    Primary's index, for what was deleted there.
    """
    ph, sh = check_census(primary), check_census(secondary)
    if ph.get("algo") != sh.get("algo"):
        raise ValueError("The two computers' fingerprints are of different kinds, so the files "
                         "cannot be compared. Install the same Ninaivu on both and take the "
                         "censuses again.")
    who = str(sh.get("name") or "the Secondary")
    roots = {PurePosixPath(t["top"].replace("\\", "/")).name: t["top"]
             for t in ph["tops"] if t["kind"] == LIBRARY}
    work = sqlite3.connect("")
    work.executescript(
        "CREATE TABLE p (folder TEXT, rel TEXT, area TEXT, digest TEXT, size INTEGER, "
        "indexed INTEGER, line TEXT);"
        "CREATE TABLE s (folder TEXT, rel TEXT, area TEXT, digest TEXT, size INTEGER, "
        "replaced INTEGER, line TEXT);")
    for table, header, path in (("p", ph, primary), ("s", sh, secondary)):
        batch = []
        for entry in entries(path):
            folder, rel = _place(header, entry)
            batch.append((folder, rel, entry["a"], entry.get("d"), int(entry["s"]),
                          int(entry.get("i" if table == "p" else "r") or 0),
                          _where(header, entry)))
            if len(batch) >= 2000:
                work.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)", batch)
                batch = []
        work.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)", batch)
    work.executescript(
        "CREATE INDEX p_place ON p (folder, rel); CREATE INDEX p_digest ON p (digest);"
        "CREATE INDEX s_digest ON s (digest);")
    totals: dict[str, Any] = {"secondary": {}, "primary": {}, "reasons": {}, "who": who}
    taken: set[tuple[str, str]] = set()                  # names given in this plan
    moved: dict[tuple[str, str], str] = {}               # a photograph put beside: where
    partial = out.with_name(f".{out.name}.partial-{secrets.token_hex(4)}")

    def count(side: str, outcome: str, size: int, reason: str | None) -> None:
        bucket = totals[side].setdefault(outcome, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += size
        if reason:
            why = totals["reasons"].setdefault(reason, {"files": 0, "bytes": 0})
            why["files"] += 1
            why["bytes"] += size

    try:
        with gzip.open(partial, "wt", encoding="utf-8") as handle:
            def say(side: str, row, outcome: str, reason: str | None = None,
                    to: str | None = None) -> None:
                line: dict[str, Any] = {"on": side, "file": row[6], "size": row[4],
                                        "outcome": outcome}
                if reason:
                    line["reason"] = reason
                if to:
                    line["to"] = to
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                count(side, outcome, int(row[4]), reason)

            for row in work.execute("SELECT * FROM s ORDER BY folder, rel").fetchall():
                folder, rel, area, digest, size, replaced = row[:6]
                if area == INBOX:
                    say("secondary", row, LEFT_ALONE, "phone_inbox")
                    continue
                if area == SET_ASIDE_AREA and replaced:
                    say("secondary", row, LEFT_ALONE, "replaced_by_primary")
                    continue
                if area == STRAY or folder not in roots:
                    say("secondary", row, LEFT_ALONE, "outside_library")
                    continue
                if area == RECYCLE:
                    say("secondary", row, LEFT_ALONE, "recycle_bin")
                    continue
                there = work.execute("SELECT digest FROM p WHERE folder = ? AND rel = ?",
                                     (folder, rel)).fetchone()
                if digest and there and there[0] == digest:
                    say("secondary", row, BOTH)
                    continue
                if digest and work.execute(
                        "SELECT 1 FROM p WHERE digest = ? AND size = ? AND indexed = 1 "
                        "AND area = ? LIMIT 1", (digest, size, LIBRARY)).fetchone():
                    say("secondary", row, BOTH)              # under another name there
                    continue
                if _deleted_here(conn, roots[folder], rel, size):
                    say("secondary", row, LEFT_ALONE, "deleted_on_primary")
                    continue
                if rel.lower().endswith(".xmp") and (folder, rel[:-4]) in moved:
                    to = f"{moved[(folder, rel[:-4])]}.xmp"
                    taken.add((folder, to))
                    say("secondary", row, BESIDE, to=f"{folder}/{to}")
                    continue
                if there is not None or (folder, rel) in taken:
                    for candidate in _besides(PurePosixPath(rel), who):
                        name = candidate.as_posix()
                        if (folder, name) not in taken and work.execute(
                                "SELECT 1 FROM p WHERE folder = ? AND rel = ?",
                                (folder, name)).fetchone() is None:
                            break
                    taken.add((folder, name))
                    moved[(folder, rel)] = name
                    say("secondary", row, BESIDE, to=f"{folder}/{name}")
                    continue
                taken.add((folder, rel))
                say("secondary", row, BROUGHT_IN, to=f"{folder}/{rel}")

            for row in work.execute("SELECT * FROM p ORDER BY folder, rel").fetchall():
                area, digest, indexed = row[2], row[3], row[5]
                if area == RECYCLE:
                    say("primary", row, LEFT_ALONE, "recycle_bin")
                elif digest and work.execute("SELECT 1 FROM s WHERE digest = ? LIMIT 1",
                                             (digest,)).fetchone():
                    say("primary", row, BOTH)
                elif indexed:
                    say("primary", row, SENT)
                else:
                    say("primary", row, LEFT_ALONE, "not_in_index")
        os.replace(partial, out)
    finally:
        partial.unlink(missing_ok=True)
        work.close()
    return totals


def read_plan(path: Path, *, outcome: str | None = None, side: str | None = None,
              after: int = 0, limit: int = 500) -> dict[str, Any]:
    """A page of a plan: the lines after line *after* that match."""
    items: list[dict[str, Any]] = []
    line_no = 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no <= after:
                continue
            item = json.loads(line)
            if (outcome and item.get("outcome") != outcome) or (side and item.get("on") != side):
                continue
            items.append({**item, "n": line_no})
            if len(items) >= limit:
                return {"items": items, "next": line_no}
    return {"items": items, "next": None}


# -- the proof -------------------------------------------------------------------

#: How many missing files the record names; all of them go to a file beside it.
NAMED = 1000


def prove(before: list[Path], after_: list[Path], missing_to: Path | None = None) -> dict[str, Any]:
    """Whether every fingerprint present in a census *before* is present in
    at least one census *after_*; the files that are not, by path.

    A file that could not be read when the census before was taken has no
    fingerprint to look for: it counts as there when a file of the same name
    in the same library folder and of the same size is there after, and is
    missing otherwise. A folder the census before could not reach (its disk
    away) means the proof cannot cover it, and does not pass.
    """
    heads_before = [check_census(p) for p in before]
    heads_after = [check_census(p) for p in after_]
    algos = {h.get("algo") for h in [*heads_before, *heads_after]}
    result: dict[str, Any] = {"before": [p.name for p in before], "after": [p.name for p in after_],
                              "proved_at": time.time(), "checked": 0, "bytes": 0,
                              "by_name": 0, "missing_count": 0, "missing": [],
                              "away_before": [t["top"] for h in heads_before for t in h["tops"]
                                              if t.get("away")]}
    if len(algos) != 1:
        result.update(passed=False, error=PROOF_KINDS["reason"])
        return result
    work = sqlite3.connect("")
    work.executescript("CREATE TABLE seen (digest TEXT PRIMARY KEY);"
                       "CREATE TABLE named (place TEXT, size INTEGER, PRIMARY KEY (place, size));")
    try:
        for header, path in zip(heads_after, after_):
            batch, names = [], []
            for entry in entries(path):
                if entry.get("d"):
                    batch.append((entry["d"],))
                names.append(("/".join(_place(header, entry)), int(entry["s"])))
                if len(names) >= 2000:
                    work.executemany("INSERT OR IGNORE INTO seen VALUES (?)", batch)
                    work.executemany("INSERT OR IGNORE INTO named VALUES (?,?)", names)
                    batch, names = [], []
            work.executemany("INSERT OR IGNORE INTO seen VALUES (?)", batch)
            work.executemany("INSERT OR IGNORE INTO named VALUES (?,?)", names)
        partial = missing_to.with_name(f".{missing_to.name}.partial") if missing_to else None
        handle = gzip.open(partial, "wt", encoding="utf-8") if partial else None
        try:
            for header, path in zip(heads_before, before):
                for entry in entries(path):
                    result["checked"] += 1
                    result["bytes"] += int(entry["s"])
                    if entry.get("d"):
                        if work.execute("SELECT 1 FROM seen WHERE digest = ?",
                                        (entry["d"],)).fetchone():
                            continue
                    elif work.execute("SELECT 1 FROM named WHERE place = ? AND size = ?",
                                      ("/".join(_place(header, entry)), int(entry["s"]))).fetchone():
                        result["by_name"] += 1
                        continue
                    gone = {"computer": header.get("name"), "role": header.get("role"),
                            "file": _where(header, entry), "size": entry["s"],
                            "digest": entry.get("d")}
                    result["missing_count"] += 1
                    if len(result["missing"]) < NAMED:
                        result["missing"].append(gone)
                    if handle is not None:
                        handle.write(json.dumps(gone, ensure_ascii=False) + "\n")
        finally:
            if handle is not None:
                handle.close()
        if partial is not None:
            if result["missing_count"]:
                os.replace(partial, missing_to)
            else:
                partial.unlink(missing_ok=True)
    finally:
        work.close()
    result["passed"] = not result["missing_count"] and not result["away_before"]
    return result


PROOF_KINDS = {"reason": "The censuses hold fingerprints of different kinds, so they cannot be "
                         "compared. Install the same Ninaivu on both computers and start again."}


# -- the record, and doing it in the background -------------------------------

#: The transition under way and the ones before it, in the state folder.
RECORD = "mugil-topology.json"
HISTORY = 20

#: Planned, carried out by the household, then proved: done or failed.
PLANNING, PLANNED, PROVING, DONE, FAILED = "planning", "planned", "proving", "done", "failed"

#: The transitions the Primary plans and proves now. Moving the Primary and
#: unpairing are checked against the standings only: after either one the
#: computer that holds the record can no longer ask the Secondary.
PLANNED_HERE = (PAIR, REPLACE_SECONDARY_DISK)

NOT_YET_PLANNED = {"reason": "A plan and a proof for this change are not made yet. Its steps "
                             "are checked on their own."}
ON_THE_PRIMARY = {"reason": "Make the plan on the Primary, once the two computers are paired: "
                            "it is the one that can ask the Secondary."}
NO_CENSUS_BEFORE = {"reason": "Take a census on this computer before it is paired: it is what "
                              "the proof compares against."}
NO_PARTNER_CENSUS = {"reason": "Take a census on the Secondary before it is paired: it is what "
                               "the proof compares against."}
BUSY = {"reason": "A census, a plan or a proof is being made already. Wait for it to finish."}
NOTHING_TO_PROVE = {"reason": "Make a plan first: the proof compares against its census."}
PROVED_ALREADY = {"reason": "That change is proved and done already."}
NOT_CARRIED_OUT = {"reason": "Carry the change out first: this computer is not where it leaves it."}


def load_record(state_dir: Path | str) -> dict[str, Any]:
    try:
        data = json.loads((Path(state_dir) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_record(state_dir: Path | str, data: dict[str, Any]) -> None:
    atomic_write(Path(state_dir) / RECORD, json.dumps(data, indent=2))


class Refused(ValueError):
    """A census, plan or proof that cannot start; the message says why."""


class Topology:
    """The census, the plan and the proof, one at a time, on a thread."""

    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection] | None = None, *,
                 partner: Callable[..., Partner] = Partner, wait: float = 5.0) -> None:
        self.cfg = cfg
        self.connect_db = connect_db
        self.partner = partner
        self.wait = wait
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.live: dict[str, Any] = {}

    # -- what it is doing ----------------------------------------------------

    @property
    def state_dir(self) -> Path:
        return Path(self.cfg.state_dir)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        here = standing(self.cfg)
        with self._lock:
            live = dict(self.live)
        return {**live, "running": self.running, "standing": asdict(here),
                "censuses": censuses(self.state_dir),
                "record": load_record(self.state_dir).get("current")}

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=30)

    def _start(self, job: str, work: Callable[[], Any]) -> dict[str, Any]:
        with self._lock:
            if self.running:
                raise Refused(BUSY["reason"])
            self._stop.clear()
            self.live = {"job": job, "started": time.time(), "progress": {}}
            self._thread = threading.Thread(target=self._run, args=(work,), daemon=True,
                                            name=f"ninaivu-mugil-{job}")
            self._thread.start()
        return self.status()

    def _run(self, work: Callable[[], Any]) -> None:
        try:
            work()
        except Stopped:
            self._tell(stopped=True)
        except (PartnerError, ValueError) as exc:
            self._tell(error=str(exc))
            self._fail_record(str(exc))
        except Exception as exc:                                  # noqa: BLE001 - said, not raised
            log.exception("The census, plan or proof stopped")
            self._tell(error=str(exc))
            self._fail_record(str(exc))
        finally:
            self._tell(finished=time.time())

    def _tell(self, **changes: Any) -> None:
        with self._lock:
            self.live.update(changes)

    def _progress(self, files: int, size: int) -> None:
        self._tell(progress={"files": files, "bytes": size})

    # -- the census -----------------------------------------------------------

    def take_census(self, read_all: bool = False) -> dict[str, Any]:
        """Take a census of this computer, in the background."""
        def work() -> None:
            made = self._census_here(read_all)
            self._tell(last=made.name)
        return self._start("census", work)

    def _census_here(self, read_all: bool = False) -> Path:
        conn = None

        def connect() -> sqlite3.Connection | None:
            nonlocal conn
            conn = self.connect_db() if self.connect_db is not None else None
            return conn
        try:
            return take_census(self.cfg, connect_db=connect, stop=self._stop.is_set,
                               progress=self._progress, read_all=read_all)
        finally:
            if conn is not None and threading.current_thread() is self._thread:
                conn.close()

    # -- talking to the Secondary -----------------------------------------------

    def _partner(self) -> Partner:
        pair = pairing.load(self.state_dir) or {}
        url = str(getattr(self.cfg, "mugil_partner_url", "") or "")
        if not pair.get("key") or not url:
            raise PartnerError("Pair this computer with the Secondary first.", 409)
        return self.partner(url, pair.get("key"), timeout=TIMEOUT)

    def _ask(self, call: Callable[[], dict[str, Any]], busy_too: bool = False) -> dict[str, Any]:
        """*call*, waiting while the Secondary is away (or busy, when asked)."""
        while True:
            try:
                answer = call()
                self._tell(waiting=None)
                return answer
            except PartnerError as exc:
                if exc.status not in AWAY and not (busy_too and exc.status == 409):
                    raise
                self._tell(waiting=str(exc))
            if self._stop.wait(self.wait):
                raise Stopped()

    def _fetch(self, partner: Partner, name: str) -> Path:
        """The Secondary's census *name*, copied here and checked whole."""
        if not _CENSUS_NAME.match(name):
            raise ValueError("The Secondary named a census that is not one.")
        target = self.state_dir / f"{PARTNER_PREFIX}{name}"
        partial = target.with_name(f".{target.name}.partial-{secrets.token_hex(4)}")
        try:
            with open(partial, "xb") as handle:
                self._ask(lambda: {"got": partner.download(
                    "/api/mugil/census/file", query={"name": name}, write=handle.write)})
            check_census(partial)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    def _partner_census(self, partner: Partner, *, fresh: bool,
                        allowed: Callable[[Standing], bool] | None = None) -> Path:
        """A census of the Secondary: one taken now (*fresh*), or the newest
        it has that was taken where *allowed* says."""
        if fresh:
            started = self._ask(lambda: partner.request("POST", "/api/mugil/census", body={}),
                                busy_too=True)
            self._tell(partner_census=started.get("job"))
            while True:
                if self._stop.wait(self.wait):
                    raise Stopped()
                there = self._ask(lambda: partner.request("GET", "/api/mugil/census"))
                if not there.get("running"):
                    break
                self._tell(partner_progress=there.get("progress"))
            if there.get("error") or not there.get("last"):
                raise ValueError(str(there.get("error") or "The Secondary did not finish its census."))
            return self._fetch(partner, str(there["last"]))
        there = self._ask(lambda: partner.request("GET", "/api/mugil/census"))
        for census in reversed(there.get("censuses") or []):
            if not isinstance(census, dict):
                continue
            if allowed is None or allowed(Standing(str(census.get("role")),
                                                   bool(census.get("paired")))):
                return self._fetch(partner, str(census.get("file")))
        raise ValueError(NO_PARTNER_CENSUS["reason"])

    # -- the plan ------------------------------------------------------------------

    def plan(self, transition: str, part: str | None = None) -> dict[str, Any]:
        """Start a plan for *transition*, made on the Primary of a pair."""
        if transition not in STARTS:
            raise Refused(NO_SUCH["reason"])
        if transition not in PLANNED_HERE:
            raise Refused(NOT_YET_PLANNED["reason"])
        here = standing(self.cfg)
        if here != Standing(PRIMARY, True):
            raise Refused(ON_THE_PRIMARY["reason"])
        if transition == PAIR:
            # The pairing has happened by now; where this computer stood
            # before it is where its census was taken.
            before = newest(self.state_dir, lambda s: why_not(PAIR, s, PRIMARY) is None)
            if before is None:
                raise Refused(NO_CENSUS_BEFORE["reason"])
            start = Standing(str(before["role"]), bool(before["paired"]))
        else:
            problem = why_not(transition, here)
            if problem:
                raise Refused(problem)
            start, before = here, None

        def work() -> None:
            ident = time.strftime("%Y%m%d-%H%M%S") + f"-{secrets.token_hex(2)}"
            record = {"id": ident, "transition": transition, "part": PRIMARY,
                      "from": asdict(start), "to": asdict(after(transition, start, PRIMARY)),
                      "state": PLANNING, "started": time.time()}
            self._put_record(record)
            partner = self._partner()
            if transition == PAIR:
                mine = self.state_dir / before["file"]
                theirs = self._partner_census(
                    partner, fresh=False, allowed=lambda s: why_not(PAIR, s, SECONDARY) is None)
            else:
                mine = self._census_here()
                theirs = self._partner_census(partner, fresh=True)
            record["before"] = {"here": mine.name, "partner": theirs.name}
            plan_file = self.state_dir / f"mugil-plan-{ident}.jsonl.gz"
            if transition == PAIR:
                conn = self.connect_db() if self.connect_db is not None else None
                try:
                    record["plan"] = plan_pair(mine, theirs, plan_file, conn=conn)
                finally:
                    if conn is not None and threading.current_thread() is self._thread:
                        conn.close()
                record["plan_file"] = plan_file.name
            else:
                # What the Secondary's disk holds that this computer does not:
                # lost with the old disk unless it is brought in first.
                only = prove([mine, theirs], [mine], missing_to=plan_file)
                record["plan"] = {"only_on_the_secondary": only["missing_count"],
                                  "files": only["checked"], "bytes": only["bytes"],
                                  "named": only["missing"]}
                record["plan_file"] = plan_file.name if only["missing_count"] else None
            record["state"] = PLANNED
            record["planned"] = time.time()
            self._put_record(record)
            self._tidy()
        return self._start("plan", work)

    def plan_page(self, **query: Any) -> dict[str, Any]:
        current = load_record(self.state_dir).get("current") or {}
        name = current.get("plan_file")
        if not name or not (self.state_dir / name).is_file():
            return {"record": current or None, "items": [], "next": None}
        page = read_plan(self.state_dir / name, **query)
        return {"record": current, **page,
                "reasons": {k: v["reason"] for k, v in REASONS.items()}}

    # -- the proof ---------------------------------------------------------------

    def prove(self) -> dict[str, Any]:
        """Take both censuses again and compare them with the ones before."""
        current = load_record(self.state_dir).get("current") or {}
        if not current.get("before") or current.get("state") not in (PLANNED, FAILED):
            if current.get("state") == DONE:
                raise Refused(PROVED_ALREADY["reason"])
            raise Refused(NOTHING_TO_PROVE["reason"])
        if asdict(standing(self.cfg)) != current.get("to"):
            raise Refused(NOT_CARRIED_OUT["reason"])

        def work() -> None:
            record = dict(current, state=PROVING)
            record.pop("error", None)
            self._put_record(record)
            partner = self._partner()
            mine = self._census_here()
            theirs = self._partner_census(partner, fresh=True)
            record["after"] = {"here": mine.name, "partner": theirs.name}
            before = [self.state_dir / record["before"]["here"],
                      self.state_dir / record["before"]["partner"]]
            missing = self.state_dir / f"mugil-missing-{record['id']}.jsonl.gz"
            record["proof"] = prove(before, [mine, theirs], missing_to=missing)
            record["missing_file"] = missing.name if record["proof"]["missing_count"] else None
            record["state"] = DONE if record["proof"]["passed"] else FAILED
            record["proved"] = time.time()
            self._put_record(record)
            self._tidy()
        return self._start("prove", work)

    # -- the record --------------------------------------------------------------

    def _put_record(self, record: dict[str, Any]) -> None:
        data = load_record(self.state_dir)
        old = data.get("current")
        if old and old.get("id") != record.get("id"):
            data["history"] = ([old] + list(data.get("history") or []))[:HISTORY]
        data["current"] = record
        _save_record(self.state_dir, data)

    def _fail_record(self, why: str) -> None:
        data = load_record(self.state_dir)
        current = data.get("current") or {}
        if current.get("state") in (PLANNING, PROVING):
            if current["state"] == PLANNING:
                # A plan that did not finish compares against nothing.
                current.pop("before", None)
            current["state"] = FAILED
            current["error"] = why
            data["current"] = current
            _save_record(self.state_dir, data)

    def _tidy(self) -> None:
        """Keep the censuses the record still compares against."""
        current = load_record(self.state_dir).get("current") or {}
        keep = {str(v) for part in ("before", "after") for v in (current.get(part) or {}).values()}
        try:
            prune(self.state_dir, keep)
        except OSError as exc:
            log.info("Old censuses could not be tidied away: %s", exc)


__all__ = ["Standing", "TRANSITIONS", "PAIR", "MOVE_PRIMARY", "REPLACE_SECONDARY_DISK", "UNPAIR",
           "STARTS", "standing", "why_not", "after", "take_census", "check_census", "entries",
           "censuses", "newest", "plan_pair", "read_plan", "prove", "load_record", "Topology",
           "Refused", "OUTCOMES", "REASONS"]
