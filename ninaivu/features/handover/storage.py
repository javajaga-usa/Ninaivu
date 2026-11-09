"""The handover plan: so the library outlives the person who runs it.

A family library kept at home has one weak point no backup covers: usually
only one person knows how it works. When that person is gone — for a while
or for good — the rest of the family has the photographs on a disk they
cannot open, backups they cannot find and a key nobody knows is needed.

So the administrator writes down who takes over and how, and Ninaivu keeps
beside it what it knows for itself (the machine, the folders, every backup
and when it last worked). That is printed and kept with the family's papers.

And it gives the successor a door: a one-time **handover code**, printed on
the sheet, that a listed successor types into their own profile to become an
administrator. The rules that make the door safe:

* Only somebody on the list can use it, signed in to their own account and
  giving their own password. A code found on the sheet by anybody else, or
  typed into somebody else's open session without the password, does nothing.
* The code is kept only as a scrypt hash, like a password, shown once when it
  is made, and used up by the first claim. Making a new one ends the old one.
* Nothing happens at once (unless the administrator chose a wait of 0 days):
  the claim waits, every administrator is told on every page, and any of them
  can cancel it. Cancelling ends that claim and the code with it.
* When the wait is over with nobody cancelling, the successor is made an
  administrator. Nobody is demoted: an administrator who is still here and
  simply missed it loses nothing.

What is *never* kept here is a password or a key. The plan says where they
are kept — "the blue folder in the steel almirah" — and :func:`secret_in`
refuses text that looks like the backup key itself was pasted in.

This module is storage and rules only; who may call what, and the activity
log, are in ``api/api_handover.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from typing import Any, Iterable

__all__ = ["SCHEMA", "TEXT_FIELDS", "WAIT_DAYS_DEFAULT", "WAIT_DAYS_MAX", "init", "load",
           "clean_plan", "secret_in", "save", "make_code", "normalise_code", "set_code",
           "code_hash", "start_claim", "claims", "waiting", "cancel", "due", "finish",
           "destinations_fingerprint", "nudges"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS handover_plan (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    plan          TEXT NOT NULL DEFAULT '{}',
    wait_days     INTEGER NOT NULL DEFAULT 7,
    reviewed_at   REAL NOT NULL DEFAULT 0,
    reviewed_by   INTEGER,
    destinations  TEXT NOT NULL DEFAULT '',
    code_hash     TEXT NOT NULL DEFAULT '',
    code_made_at  REAL NOT NULL DEFAULT 0,
    code_made_by  INTEGER
);
CREATE TABLE IF NOT EXISTS handover_claims (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    requested_at  REAL NOT NULL,
    due_at        REAL NOT NULL,
    state         TEXT NOT NULL DEFAULT 'waiting',
    ended_at      REAL NOT NULL DEFAULT 0,
    ended_by      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_handover_claims_state ON handover_claims(state, due_at);
"""

#: What the administrator writes, and how long each may be. Generous for a
#: letter, short enough that the sheet stays a few pages.
TEXT_FIELDS = {
    "message": 4000,          # to the successors, in the administrator's words
    "backups_where": 1500,    # where the backup disks and accounts are
    "secrets_where": 1000,    # where the backup key's recovery papers are KEPT
    "password_where": 1000,   # where the computer's password is KEPT
    "machine_access": 1500,   # how to reach the machine: where it is, how to switch it on
    "hardware": 1500,         # what the machine and its disks are
}
MAX_SUCCESSORS = 5
MAX_HELPERS = 6

WAIT_DAYS_DEFAULT = 7
WAIT_DAYS_MAX = 30
DAY = 86400.0

#: A plan reviewed longer ago than this is worth a second look.
REVIEW_EVERY = 182 * DAY

#: Read aloud over the phone, written on paper, typed on a phone: no 0/O, 1/I/L,
#: or U/V, and upper case only. Sixteen of thirty characters is about 78 bits,
#: far past guessing at the few tries a day the claim allows.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZ23456789"
CODE_LENGTH = 16

WAITING, CANCELLED, DONE, VOID = "waiting", "cancelled", "done", "void"


def init(conn: sqlite3.Connection) -> None:
    """Made with the index (``db.init_db``); here for a database made before."""
    conn.executescript(SCHEMA)


# -- the plan --------------------------------------------------------------

def _record(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM handover_plan WHERE id=1").fetchone()


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    """The plan as kept, with an empty one when none has been written. Never
    the code's hash: only whether there is a code and when it was made."""
    row = _record(conn)
    plan: dict[str, Any] = {}
    if row is not None:
        try:
            plan = json.loads(row["plan"] or "{}")
        except ValueError:
            plan = {}
    if not isinstance(plan, dict):
        plan = {}
    out = {name: str(plan.get(name) or "") for name in TEXT_FIELDS}
    out["successors"] = [int(s) for s in plan.get("successors") or [] if isinstance(s, int)]
    out["helpers"] = [h for h in plan.get("helpers") or [] if isinstance(h, dict)]
    return {
        "plan": out,
        "wait_days": int(row["wait_days"]) if row is not None else WAIT_DAYS_DEFAULT,
        "reviewed_at": float(row["reviewed_at"]) if row is not None else 0.0,
        "reviewed_by": row["reviewed_by"] if row is not None else None,
        "destinations": str(row["destinations"]) if row is not None else "",
        "has_code": bool(row is not None and row["code_hash"]),
        "code_made_at": float(row["code_made_at"]) if row is not None else 0.0,
    }


#: A run of base64 long enough to be a key rather than a word. The backup key
#: is 32 bytes: 44 characters of base64, 64 of hex.
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
#: A recovery file pasted whole.
_RECOVERY = re.compile(r'"key"\s*:\s*"|ninaivu-encrypted-backup', re.I)


def _random_looking(run: str) -> bool:
    """Letters, digits and case changes everywhere: a key, not a folder name.

    A path such as ``/Volumes/PhotoBackup2/Family`` is one long run of the
    base64 alphabet too, so the length alone would refuse the very thing the
    plan asks for. What tells them apart is how often the case flips: about
    one pair in three in random base64, a handful in a whole path of words.
    """
    letters = [c for c in run if c.isalpha()]
    if not any(c.isdigit() for c in run) or not letters:
        return False
    flips = sum(1 for a, b in zip(letters, letters[1:]) if a.isupper() != b.isupper())
    return flips >= len(run) // 6


def secret_in(text: str, known: Iterable[str] = ()) -> bool:
    """Whether *text* looks like it holds the backup key (or another secret
    Ninaivu knows) rather than saying where it is kept.

    *known* are the actual secrets on this machine — the backup key in base64
    and hex, the off-site store's secret key — matched exactly. The shape test
    catches the rest: a long run of random-looking base64, 32 or more hex
    digits together, or a recovery file pasted whole. Not a password: nothing
    can tell one from a sentence, which is why the page says so in words.
    """
    if not text:
        return False
    for secret in known:
        if secret and len(secret) >= 8 and secret in text:
            return True
    if _RECOVERY.search(text):
        return True
    for found in _HEX_RUN.finditer(text):
        run = found.group(0)
        if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
            return True
    return any(_random_looking(found.group(0)) for found in _BASE64_RUN.finditer(text))


def clean_plan(raw: Any, *, eligible: Iterable[int], known_secrets: Iterable[str] = ()
               ) -> dict[str, Any]:
    """Only the fields a plan has, each the type it must be. ValueError otherwise.

    *eligible* are the family members who may be named as successors.
    """
    if not isinstance(raw, dict):
        raise ValueError("The plan must be an object.")
    known = [s for s in known_secrets if s]
    plan: dict[str, Any] = {}
    for name, limit in TEXT_FIELDS.items():
        value = raw.get(name) or ""
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text.")
        value = value.strip()
        if len(value) > limit:
            raise ValueError(f"{name} can be up to {limit} characters.")
        if secret_in(value, known):
            raise ValueError("That looks like a key or a password itself. Write where it "
                             "is kept, never the key or the password.")
        plan[name] = value

    wanted = raw.get("successors") or []
    if not isinstance(wanted, list) or not all(isinstance(s, int) and not isinstance(s, bool)
                                               for s in wanted):
        raise ValueError("successors must be a list of profile ids.")
    allowed = set(int(e) for e in eligible)
    successors: list[int] = []
    for person in wanted:
        if person not in allowed:
            raise ValueError("A successor must be a family member's profile.")
        if person not in successors:
            successors.append(person)
    if len(successors) > MAX_SUCCESSORS:
        raise ValueError(f"Name up to {MAX_SUCCESSORS} successors.")
    plan["successors"] = successors

    helpers = raw.get("helpers") or []
    if not isinstance(helpers, list) or len(helpers) > MAX_HELPERS:
        raise ValueError(f"helpers must be a list of up to {MAX_HELPERS}.")
    kept = []
    for helper in helpers:
        if not isinstance(helper, dict):
            raise ValueError("Each helper has a name and a phone number.")
        name = helper.get("name") or ""
        phone = helper.get("phone") or ""
        if not isinstance(name, str) or not isinstance(phone, str) \
                or len(name) > 80 or len(phone) > 40:
            raise ValueError("A helper's name is up to 80 characters and the phone up to 40.")
        if secret_in(name, known) or secret_in(phone, known):
            raise ValueError("That looks like a key or a password itself. Write where it "
                             "is kept, never the key or the password.")
        if name.strip() or phone.strip():
            kept.append({"name": name.strip(), "phone": phone.strip()})
    plan["helpers"] = kept
    return plan


def clean_wait_days(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= WAIT_DAYS_MAX:
        raise ValueError(f"The waiting period is 0 to {WAIT_DAYS_MAX} days.")
    return value


def save(conn: sqlite3.Connection, plan: dict[str, Any], *, wait_days: int, user_id: int,
         destinations: str, now: float | None = None) -> None:
    """Keep a cleaned plan. Saving it is reviewing it: the date, and the
    backups it was reviewed against, are what the nudge compares with."""
    when = time.time() if now is None else now
    init(conn)
    conn.execute(
        "INSERT INTO handover_plan(id, plan, wait_days, reviewed_at, reviewed_by, destinations) "
        "VALUES(1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET plan=excluded.plan, "
        "wait_days=excluded.wait_days, reviewed_at=excluded.reviewed_at, "
        "reviewed_by=excluded.reviewed_by, destinations=excluded.destinations",
        (json.dumps(plan, sort_keys=True), int(wait_days), when, int(user_id), destinations))
    conn.commit()


# -- the code ----------------------------------------------------------------

def make_code() -> str:
    """``ABCD-EFGH-JKMN-PQRS``: four groups of four, said a group at a time."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LENGTH, 4))


def normalise_code(text: Any) -> str:
    """What was typed, as the code is kept: upper case, no spaces or dashes."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"[^A-Z0-9]", "", text.upper())[:64]


def code_hash(conn: sqlite3.Connection) -> str:
    row = _record(conn)
    return str(row["code_hash"]) if row is not None else ""


def set_code(conn: sqlite3.Connection, user_id: int, hashed: str,
             now: float | None = None) -> None:
    """Keep a new code's hash. The old one, if any, stops working here."""
    when = time.time() if now is None else now
    init(conn)
    conn.execute(
        "INSERT INTO handover_plan(id, wait_days, code_hash, code_made_at, code_made_by) "
        "VALUES(1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET code_hash=excluded.code_hash, "
        "code_made_at=excluded.code_made_at, code_made_by=excluded.code_made_by",
        (WAIT_DAYS_DEFAULT, hashed, when, int(user_id)))
    conn.commit()


# -- claims ------------------------------------------------------------------

def start_claim(conn: sqlite3.Connection, user_id: int, hashed: str, *,
                now: float | None = None) -> dict[str, Any] | None:
    """Use the code up and start the wait, as one write.

    *hashed* is the hash the caller checked the code against. The code is
    cleared only if it is still that one, so two claims racing with the same
    code cannot both start, and a code replaced in between is not used.
    None when the code was used or replaced first.
    """
    when = time.time() if now is None else now
    try:
        updated = conn.execute(
            "UPDATE handover_plan SET code_hash='' WHERE id=1 AND code_hash=? AND code_hash!=''",
            (hashed,))
        if updated.rowcount != 1:
            conn.rollback()
            return None
        wait = int(conn.execute("SELECT wait_days FROM handover_plan WHERE id=1").fetchone()[0])
        cur = conn.execute(
            "INSERT INTO handover_claims(user_id, requested_at, due_at, state) VALUES(?,?,?,?)",
            (int(user_id), when, when + wait * DAY, WAITING))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return get_claim(conn, int(cur.lastrowid))


def get_claim(conn: sqlite3.Connection, claim_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM handover_claims WHERE id=?", (int(claim_id),)).fetchone()
    return dict(row) if row else None


def claims(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """The latest claims, newest first, whatever became of them."""
    rows = conn.execute("SELECT * FROM handover_claims ORDER BY id DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]


def waiting(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM handover_claims WHERE state=? ORDER BY due_at",
                        (WAITING,))
    return [dict(r) for r in rows]


def cancel(conn: sqlite3.Connection, claim_id: int, by: int, now: float | None = None) -> bool:
    """End a waiting claim. False if it was not waiting (already done, cancelled)."""
    when = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE handover_claims SET state=?, ended_at=?, ended_by=? WHERE id=? AND state=?",
        (CANCELLED, when, int(by), int(claim_id), WAITING))
    conn.commit()
    return cur.rowcount == 1


def due(conn: sqlite3.Connection, now: float | None = None) -> list[dict[str, Any]]:
    """Waiting claims whose wait is over."""
    when = time.time() if now is None else now
    rows = conn.execute("SELECT * FROM handover_claims WHERE state=? AND due_at<=? ORDER BY id",
                        (WAITING, when))
    return [dict(r) for r in rows]


def finish(conn: sqlite3.Connection, claim_id: int, state: str,
           now: float | None = None) -> bool:
    """Close a waiting claim as done or void. False if somebody closed it first."""
    when = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE handover_claims SET state=?, ended_at=? WHERE id=? AND state=?",
        (state, when, int(claim_id), WAITING))
    conn.commit()
    return cur.rowcount == 1


# -- keeping it current --------------------------------------------------------

def destinations_fingerprint(destinations: Iterable[dict[str, Any]]) -> str:
    """Which backups there are and where, as one short value. Times are left
    out: a backup that ran again is the same backup, one that moved is not."""
    keys = sorted(f"{d.get('kind', '')}|{d.get('where', '')}" for d in destinations)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def nudges(record: dict[str, Any], fingerprint: str, now: float | None = None) -> list[str]:
    """Why the plan wants a look, if it does: ``none`` (never written),
    ``old`` (not reviewed for six months), ``backups`` (the backups changed
    since it was reviewed)."""
    when = time.time() if now is None else now
    if not record.get("reviewed_at"):
        return ["none"]
    found = []
    if when - float(record["reviewed_at"]) > REVIEW_EVERY:
        found.append("old")
    if record.get("destinations") != fingerprint:
        found.append("backups")
    return found
