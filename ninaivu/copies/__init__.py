"""One contract for every copy of the library (4.0 proposal 4).

Ninaivu keeps copies of the household's photographs in several places, and
each grew up on its own: the second disk (storage/mirror.py), the Secondary
computer (mugil/courier.py), the encrypted copy away from home
(cloud/offsite.py) and Google Drive (cloud/service.py). Each has its own
engine, schedule, record and progress, and the console asked each one in its
own words. A household cannot see from that whether a photograph is kept
anywhere but the library, and every new kind of copy was one more of
everything.

This module is the one way to ask. A :class:`Destination` says:

* its **name** in the ledger and its **kind** (``folder``, ``secondary``,
  ``s3``, ``drive``);
* what it **keeps**: the originals, the index, the family's view;
* whether it **encrypts**. A copy that leaves the house must, so a ``s3`` or
  ``drive`` destination that does not is a problem it says out loud
  (:func:`problems`), whatever its engine does;
* its **schedule**, its **status** (one shape for all of them), what the
  **ledger** says it keeps (ninaivu/copies/ledger.py), and a
  **restore_test** hook that proves a copy can be read back.

In this stage the adapters wrap the engines as they are and change nothing
about what any of them does: every call goes to an engine's own public
method, or reads what it already records. Moving the engines onto the
contract, so that each writes the ledger itself, is later work.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol, runtime_checkable

from . import ledger

__all__ = ["Destination", "MirrorDestination", "SecondaryDestination", "OffsiteDestination",
           "DriveDestination", "KINDS", "KEEPS", "MUST_ENCRYPT", "ORIGINALS", "INDEX",
           "FAMILY_VIEW", "FILL_EVERY", "problems", "destinations", "refresh", "report"]

#: What a destination may be.
KINDS = ("folder", "secondary", "s3", "drive")

ORIGINALS, INDEX, FAMILY_VIEW = "originals", "index", "family view"
#: What a destination may keep.
KEEPS = (ORIGINALS, INDEX, FAMILY_VIEW)

#: The kinds that are always somewhere else, so always encrypted (rule 1:
#: nothing leaves the house unless the administrator chose where it goes, and
#: anything that does is encrypted).
MUST_ENCRYPT = frozenset({"s3", "drive"})


@runtime_checkable
class Destination(Protocol):
    """A place a copy of the library is kept."""

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def keeps(self) -> frozenset[str]: ...

    @property
    def encrypts(self) -> bool: ...

    @property
    def schedule(self) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def ledger(self, conn: sqlite3.Connection) -> dict[str, int]: ...

    def holds(self, conn: sqlite3.Connection, fingerprint: str) -> bool: ...

    def restore_test(self) -> dict[str, Any]: ...


def problems(dest: Destination) -> list[str]:
    """What breaks the contract, in words for the console."""
    out = []
    if dest.kind not in KINDS:
        out.append(f"{dest.name} is a kind of copy Ninaivu does not know ({dest.kind}).")
    if not dest.keeps <= set(KEEPS):
        out.append(f"{dest.name} says it keeps something Ninaivu does not know.")
    if dest.kind in MUST_ENCRYPT and not dest.encrypts:
        out.append("This copy leaves the house unencrypted. Turn encryption back on.")
    return out


class _Adapter:
    """What every adapter shares: the ledger reads, and the one status shape."""

    name = ""
    kind = ""
    keeps: frozenset[str] = frozenset()
    encrypts = False
    schedule: dict[str, Any] = {}

    def ledger(self, conn: sqlite3.Connection) -> dict[str, int]:
        return ledger.counts(conn, self.name)

    def holds(self, conn: sqlite3.Connection, fingerprint: str) -> bool:
        return ledger.holds(conn, self.name, fingerprint)

    def _shape(self, *, on: bool, running: bool, problem: str | None, last_finished: float,
               error: str, **extra: Any) -> dict[str, Any]:
        """The status every destination answers with: the same keys, so the
        console reads them all one way. What is particular to one goes in
        ``detail``."""
        said = [p for p in (problem,) if p] + problems(self)
        return {"name": self.name, "kind": self.kind, "on": bool(on),
                "keeps": sorted(self.keeps), "encrypts": bool(self.encrypts),
                "schedule": self.schedule, "running": bool(running),
                "problems": said, "last_finished": float(last_finished or 0),
                "error": str(error or ""), "detail": extra}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


class MirrorDestination(_Adapter):
    """The second disk: a plain copy of the originals in a folder at home."""

    name, kind = "mirror", "folder"
    keeps = frozenset({ORIGINALS})
    encrypts = False

    def __init__(self, mirror) -> None:
        self.engine = mirror

    @property
    def schedule(self) -> dict[str, Any]:
        return {"every_hours": self.engine.every / 3600,
                "check_every_days": self.engine.verify_every / 86400,
                "automatic": self.engine.every > 0}

    def status(self) -> dict[str, Any]:
        s = self.engine.status()
        return self._shape(on=s["enabled"], running=s["running"], problem=s["problem"],
                           last_finished=s["last_finished"], error=s["error"],
                           where=s["folder"], last_verified=s["last_verified"])

    def restore_test(self) -> dict[str, Any]:
        """Every file on the disk read back and compared with its fingerprint."""
        return {**self.engine.verify(), "test": "every file read back and checked"}


class SecondaryDestination(_Adapter):
    """The Secondary computer: every original, kept exactly as it was sent,
    and, when the household turned it on, the family's read-only view."""

    name, kind = "secondary", "secondary"
    #: Plain files on a computer in the house, as the second disk's are; the
    #: pairing signs every request between the two.
    encrypts = False

    def __init__(self, courier, handover=None) -> None:
        self.engine = courier
        self.handover = handover
        self.cfg = courier.cfg

    @property
    def keeps(self) -> frozenset[str]:
        if getattr(self.cfg, "mugil_family_copy", False):
            return frozenset({ORIGINALS, FAMILY_VIEW})
        return frozenset({ORIGINALS})

    @property
    def schedule(self) -> dict[str, Any]:
        every = float(getattr(self.cfg, "mugil_copy_every_hours", 0) or 0)
        return {"every_hours": every, "automatic": every > 0}

    def _paired(self) -> bool:
        from ..mugil import PRIMARY, pairing, role                    # noqa: PLC0415
        return role(self.cfg) == PRIMARY and pairing.load(self.cfg.state_dir) is not None

    def status(self) -> dict[str, Any]:
        s = self.engine.status()
        last = s.get("last") or {}
        problem = None if s["running"] else self.engine.ready()
        extra: dict[str, Any] = {"where": str(getattr(self.cfg, "mugil_partner_url", "") or "")}
        if self.handover is not None:
            view = self.handover.status()
            extra["family_view"] = {"on": view.get("on", False), "at": view.get("at"),
                                    "error": view.get("error", "")}
        return self._shape(on=self._paired(), running=s["running"],
                           problem=problem if self._paired() else None,
                           last_finished=float(last.get("finished") or 0),
                           error=str(last.get("error") or ""), **extra)

    def restore_test(self) -> dict[str, Any]:
        return {"started": False, "test": "",
                "reason": "A test restore from the Secondary is not written yet; every file "
                          "it keeps was checked against its fingerprint as it arrived."}


class OffsiteDestination(_Adapter):
    """The encrypted copy away from home: an S3 bucket or a folder elsewhere."""

    name = "offsite"
    keeps = frozenset({ORIGINALS})
    #: Always: the off-site copy refuses to start without the backup key, and
    #: encrypts the names as well as the bytes.
    encrypts = True

    def __init__(self, offsite) -> None:
        self.engine = offsite
        self.cfg = offsite.cfg

    @property
    def kind(self) -> str:
        return "s3" if str(getattr(self.cfg, "offsite_kind", "folder")) == "s3" else "folder"

    @property
    def schedule(self) -> dict[str, Any]:
        every = max(1.0, float(getattr(self.cfg, "offsite_every_hours", 24) or 24))
        return {"every_hours": every, "automatic": True}

    def where(self) -> str:
        """The destination now chosen, as the engine records it (its
        ``dest_where``), so the ledger does not count what went elsewhere."""
        return self.engine._where()                                 # noqa: SLF001

    def status(self) -> dict[str, Any]:
        s = self.engine.status()
        return self._shape(on=s["enabled"], running=s["running"], problem=s["problem"],
                           last_finished=s["last_run"], error=s["error"], where=s["where"])

    def restore_test(self) -> dict[str, Any]:
        """A small file written there, read back and compared. Not yet a
        sample of the photographs restored, as Drive's is."""
        try:
            done = self.engine.test()
        except (ValueError, OSError) as exc:
            return {"started": False, "reason": str(exc), "test": "write and read back"}
        return {"started": True, "ok": bool(done.get("ok")), "test": "write and read back"}


class DriveDestination(_Adapter):
    """Google Drive: the originals, and the index beside them."""

    name, kind = "drive", "drive"

    def __init__(self, cloud, *, index_copy=None, restore_tests=None) -> None:
        self.engine = cloud
        self.index_copy = index_copy
        self.restore_tests = restore_tests
        self.cfg = cloud.cfg

    @property
    def keeps(self) -> frozenset[str]:
        if self.index_copy is not None and self.index_copy.every_hours > 0:
            return frozenset({ORIGINALS, INDEX})
        return frozenset({ORIGINALS})

    @property
    def encrypts(self) -> bool:
        return bool(getattr(self.cfg, "cloud_encrypt", True))

    @property
    def schedule(self) -> dict[str, Any]:
        # The upload follows the library rather than a clock: it goes when
        # something is owed, inside the hours the household allowed.
        return {"every_hours": 0.0, "automatic": bool(getattr(self.cfg, "cloud_autostart", False)),
                "window": [str(self.cfg.cloud_window_start or ""),
                           str(self.cfg.cloud_window_end or "")]}

    def status(self) -> dict[str, Any]:
        s = self.engine.status()
        state = s.get("state") or {}
        queue = s.get("queue") or {}
        problem = None
        if state.get("needs_reconnect"):
            problem = "Google asks for the account to be connected again."
        return self._shape(on=s["enabled"], running=s["running"], problem=problem,
                           last_finished=0.0, error=str(state.get("last_error") or ""),
                           where=s.get("folder_name", ""),
                           sent_unencrypted=int(queue.get("unencrypted", 0)))

    def restore_test(self) -> dict[str, Any]:
        """A few photographs restored from Drive into a temporary folder and
        compared byte for byte (cloud/restore_test.py)."""
        if self.restore_tests is None:
            return {"started": False, "reason": "There is no test restore here.", "test": ""}
        started = self.restore_tests.run_in_background()
        return {"started": bool(started), "test": "a sample restored and compared",
                **({} if started else {"reason": "A test restore is already running."})}


def destinations(services) -> list[Destination]:
    """Every destination of this Ninaivu, from its running engines."""
    return [
        MirrorDestination(services.mirror),
        SecondaryDestination(services.mugil_courier, getattr(services, "mugil_handover", None)),
        OffsiteDestination(services.offsite),
        DriveDestination(services.cloud, index_copy=getattr(services, "index_copy", None),
                         restore_tests=getattr(services, "restore_tests", None)),
    ]


#: How long the ledger is taken as it was filled before the console's read
#: fills it again from the engines' records, in seconds. Filling reads every
#: record once (about a second at 200,000 photographs), so not on every look.
FILL_EVERY = 5 * 60.0

_filled: dict[str, float] = {}


def refresh(conn: sqlite3.Connection, dests: list[Destination], state_dir, *,
            now: float, force: bool = False) -> bool:
    """Fill the ledger again from the engines' records, if it was not filled
    in the last :data:`FILL_EVERY` seconds by this process. Returns whether it was."""
    key = str(ledger.state_dir_of(conn) or id(conn))
    if not force and now - _filled.get(key, 0.0) < FILL_EVERY:
        return False
    where = next((d.where() for d in dests if isinstance(d, OffsiteDestination)), None)
    ledger.refresh(conn, state_dir=state_dir, offsite_where=where)
    _filled[key] = now
    return True


def report(conn: sqlite3.Connection, roots: list[str], dests: list[Destination]) -> dict[str, Any]:
    """Each destination's status and what the ledger says it keeps, and the
    headline: photographs in at least two places, of all of them."""
    out = []
    for dest in dests:
        out.append({**dest.status(), "ledger": dest.ledger(conn)})
    return {"headline": ledger.headline(conn, roots), "destinations": out}
