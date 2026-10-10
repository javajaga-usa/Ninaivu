"""The handover plan and the takeover code (storage/handover.py).

Three audiences, three sets of routes:

* the administrator, on the console only (``admin_only``): read and save the
  plan, make a handover code, print the sheet;
* every administrator, on either app (``bp``): see a claim that is waiting
  and cancel it — the banner has to reach whoever is still around, wherever
  they happen to look;
* a family member (``bp``): whether they are named as a successor, and the
  claim itself.

Nothing here talks to anything outside this computer. What Ninaivu fills in
about itself is read from its own settings and records; the backup key is
named by its fingerprint and never by itself.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from flask import Response, abort, current_app, jsonify, request

from .. import __version__
from ..server import auth
from ..server.auth import ROLE_ADMIN, ROLE_FAMILY, current_user, require_admin, require_family
from ..server.config import house_name
from ..storage import handover
from ..words import filled, said
from ._body import json_object
from .api import _cfg, _conn, admin_only, bp

log = logging.getLogger(__name__)

LOCALES = Path(__file__).resolve().parent.parent / "static" / "i18n"
LANGUAGES = ("en", "ta")


def _services() -> Any:
    return current_app.config.get("MV_SERVICES")


# ---------------------------------------------------------------------------
# What Ninaivu knows for itself
# ---------------------------------------------------------------------------

def _meta(conn: sqlite3.Connection, table: str, key: str) -> float:
    """A time from one of the copies' own record tables, 0 when there is none
    (the table is made the first time that copy runs)."""
    try:
        row = conn.execute(f"SELECT value FROM {table} WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row and row[0] else 0.0
    except (sqlite3.Error, ValueError, TypeError):
        return 0.0


def destinations(cfg: Any, services: Any, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every place a copy of the library or the index goes, with when it last
    finished. Read from settings and records only: nothing is contacted, and
    no secret is read, so this is safe to show and to print."""
    from ..storage import backup                                  # noqa: PLC0415
    found: list[dict[str, Any]] = []

    keeper = getattr(services, "backups", None)
    folder = keeper.folder if keeper is not None else (
        Path(cfg.backup_dir) if getattr(cfg, "backup_dir", "") else Path(cfg.state_dir) / "backups")
    last = backup.latest(folder)
    found.append({"kind": "index", "title": said("Copies of the index and settings"),
                  "where": str(folder), "last_ok": float(last["at"]) if last else 0.0})

    if getattr(cfg, "mirror_enabled", False):
        found.append({"kind": "mirror", "title": said("Second copy on another disk"),
                      "where": str(getattr(cfg, "mirror_dir", "") or ""),
                      "last_ok": _meta(conn, "mirror_meta", "last_finished")})

    if getattr(cfg, "offsite_enabled", False):
        if str(getattr(cfg, "offsite_kind", "folder") or "folder") == "s3":
            where = "/".join(part for part in (
                str(getattr(cfg, "offsite_endpoint", "") or "").rstrip("/"),
                str(getattr(cfg, "offsite_bucket", "") or ""),
                str(getattr(cfg, "offsite_prefix", "") or "").strip("/")) if part)
        else:
            from ..cloud.offsite import FolderTarget                # noqa: PLC0415
            chosen = str(getattr(cfg, "offsite_folder", "") or "")
            where = FolderTarget(chosen).describe() if chosen else ""
        found.append({"kind": "offsite", "title": said("Encrypted off-site copy"),
                      "where": where, "last_ok": _meta(conn, "offsite_meta", "last_run")})

    cloud = getattr(services, "cloud", None)
    creds = getattr(cloud, "creds", None)
    if getattr(cfg, "cloud_enabled", False) or getattr(creds, "connected", False):
        account = str(getattr(creds, "account", "") or "")
        drive_folder = str(getattr(creds, "folder_name", "")
                           or getattr(cfg, "cloud_folder_name", "") or "Ninaivu")
        try:
            row = conn.execute("SELECT MAX(done_at) FROM cloud_uploads WHERE state='done'"
                               ).fetchone()
            sent = float(row[0] or 0)
        except sqlite3.Error:
            sent = 0.0
        found.append({"kind": "drive", "title": said("Mugil: Google Drive"),
                      "where": " · ".join(p for p in (account, drive_folder) if p),
                      "last_ok": sent})
    return found


def _key_record(cfg: Any) -> dict[str, Any] | None:
    from ..cloud import keyring                                   # noqa: PLC0415
    try:
        return keyring.load(cfg.state_dir)
    except Exception:                                             # noqa: BLE001
        return None


def key_fingerprint(cfg: Any) -> str:
    """The backup key's id, in groups of four: enough to tell that a recovery
    file is the right one, and nothing anybody could open a backup with."""
    record = _key_record(cfg)
    if not record:
        return ""
    raw = str(record.get("key_id") or "").upper()
    return " ".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def known_secrets(cfg: Any, services: Any) -> list[str]:
    """The secrets on this machine, for refusing a plan that has one pasted in.
    Used only for that comparison; never sent anywhere or stored."""
    found: list[str] = []
    record = _key_record(cfg)
    if record and record.get("key"):
        found.append(str(record["key"]))
        try:
            found.append(base64.b64decode(record["key"]).hex())
        except ValueError:
            pass
    offsite = getattr(services, "offsite", None)
    if offsite is not None:
        try:
            found.append(offsite.secret())
        except Exception:                                         # noqa: BLE001
            pass
    creds = getattr(getattr(services, "cloud", None), "creds", None)
    for name in ("client_secret", "refresh_token"):
        found.append(str(getattr(creds, name, "") or ""))
    found.append(str(getattr(cfg, "notify_smtp_password", "") or ""))
    return [s for s in found if s]


def remote_access(cfg: Any, services: Any) -> dict[str, Any]:
    from ..server import remote                                   # noqa: PLC0415
    try:
        cached = getattr(services, "_remote", None)
        resolved = cached[1] if cached else remote.resolve(cfg)
    except Exception:                                             # noqa: BLE001
        return {"name": "none", "title": "", "hostnames": []}
    return {"name": resolved.name, "title": "" if resolved.name == "none" else resolved.title,
            "hostnames": list(resolved.hostnames)}


#: The way back, in the order it is done. A summary of docs/backup-recovery.md
#: and docs/moving-to-another-machine.md for somebody who has never opened
#: either, and printed whether or not the backups above exist.
RESTORE_STEPS = (
    said("Keep the computer as it is. Do not reinstall it or wipe its disks: the library folders and Ninaivu's own folder on it are the photographs and everything Ninaivu knows about them."),
    said("If the computer still works, switch it on, open the console (the Ninaivu Control Panel, or the address on this sheet), sign in as administrator and look at the Backup & health pages."),
    said("If the computer is gone, install Ninaivu on another one. Copy the library folders back from the second copy or the off-site copy, and copy Ninaivu's own folder, or bring back the newest copy of the index with: ninaivu restore <bundle.tar.gz>"),
    said("If the library is now at a different place, point the index at it on the console's Move to another computer page (or with ninaivu reroot) rather than letting it index everything again."),
    said("The encrypted copies (the off-site copy and Mugil on Google Drive) need the backup key: bring it back on the Mugil page from the recovery file, or from the passphrase. Without one of them nobody can read those copies, Ninaivu included."),
    said("The off-site copy can be read without Ninaivu running: ninaivu offsite-restore --recovery <recovery file> <copy> <output folder>"),
    said("Then connect Google again on the Mugil page, and check that every backup runs."),
)


def facts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything the sheet says that Ninaivu knows without being told."""
    cfg = _cfg()
    services = _services()
    return {
        "home": house_name(cfg),
        "machine": socket.gethostname(),
        "version": __version__,
        "libraries": list(cfg.library_roots),
        "state_dir": str(cfg.state_dir),
        "destinations": destinations(cfg, services, conn),
        "key_fingerprint": key_fingerprint(cfg),
        "remote": remote_access(cfg, services),
        "restore_steps": list(RESTORE_STEPS),
    }


def current_nudges(conn: sqlite3.Connection, now: float | None = None) -> list[str]:
    """Why the plan wants a look right now, if it does (``handover.nudges``)."""
    fingerprint = handover.destinations_fingerprint(
        destinations(_cfg(), _services(), conn))
    return handover.nudges(handover.load(conn), fingerprint, now)


# ---------------------------------------------------------------------------
# Making a successor an administrator when the wait is over
# ---------------------------------------------------------------------------

def _eligible(conn: sqlite3.Connection) -> list[auth.User]:
    """Who may be named a successor: family members whose profiles are on."""
    return [u for u in auth.list_users(conn, include_inactive=False) if u.role == ROLE_FAMILY]


def complete_due(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Finish every claim whose wait is over; how many became administrators.

    Checked again here rather than trusted from the claim: somebody taken off
    the list, switched off or deleted while it waited does not become an
    administrator. Each claim is closed before the role is written, so two
    requests arriving together cannot both act on it.
    """
    made = 0
    for claim in handover.due(conn, now):
        user = auth.get_user(conn, int(claim["user_id"]))
        successors = handover.load(conn)["plan"]["successors"]
        # A profile made after the claim began is not the person who made it,
        # whatever its number (profile ids are reused after a deletion).
        fit = (user is not None and user.active and user.id in successors
               and float(user.created_at or 0) <= float(claim["requested_at"])
               and user.role in (ROLE_FAMILY, ROLE_ADMIN) and user.has_password)
        if not fit:
            if handover.finish(conn, claim["id"], handover.VOID, now):
                auth.audit(conn, claim["user_id"], "handover_void",
                           f"{user.username if user else claim['user_id']}: no longer a "
                           "successor who can sign in, so nothing was changed")
            continue
        if not handover.finish(conn, claim["id"], handover.DONE, now):
            continue
        if user.role != ROLE_ADMIN:
            auth.update_profile(conn, user.id, role=ROLE_ADMIN)
            # As when an administrator promotes somebody: whatever let them
            # in so far (a tap, a PIN) is not how an administrator signs in.
            auth.end_all_sessions(conn, user.id)
        auth.audit(conn, user.id, "handover_complete",
                   f"{user.username} is now an administrator (handover plan)")
        made += 1
    return made


#: When each database was last looked at for a claim that is due.
_CHECKED: dict[str, float] = {}
#: Often enough that a successor waiting at the screen is not kept waiting;
#: seldom enough that a page of thumbnails is not a page of extra queries.
CHECK_EVERY = 30.0


@bp.before_app_request
def _promote_when_due():
    """The deadline is acted on by the next request after it, on either app.

    Ninaivu has no scheduler outside itself, and needs none: a claim matters
    only to somebody about to use Ninaivu, and their first request finishes
    it. The first request after a restart counts too.
    """
    try:
        path = str(_cfg().db_path)
    except (KeyError, RuntimeError):
        return None
    now = time.monotonic()
    if now - _CHECKED.get(path, -CHECK_EVERY) < CHECK_EVERY:
        return None
    _CHECKED[path] = now
    try:
        complete_due(_conn())
    except sqlite3.Error as exc:
        log.warning("handover: could not check for a claim that is due: %s", exc)
    return None


# ---------------------------------------------------------------------------
# The administrator's page
# ---------------------------------------------------------------------------

def _names(conn: sqlite3.Connection) -> dict[int, str]:
    return {u.id: u.display_name for u in auth.list_users(conn)}


def _claim_out(claim: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    return {"id": claim["id"], "user_id": claim["user_id"],
            "name": names.get(int(claim["user_id"]), ""),
            "state": claim["state"], "requested_at": claim["requested_at"],
            "due_at": claim["due_at"], "ended_at": claim["ended_at"]}


def _page(conn: sqlite3.Connection) -> dict[str, Any]:
    complete_due(conn)
    record = handover.load(conn)
    names = _names(conn)
    found = facts(conn)
    fingerprint = handover.destinations_fingerprint(found["destinations"])
    return {
        "plan": record["plan"],
        "wait_days": record["wait_days"],
        "max_wait_days": handover.WAIT_DAYS_MAX,
        "reviewed_at": record["reviewed_at"],
        "reviewed_by": names.get(int(record["reviewed_by"] or 0), ""),
        "has_code": record["has_code"],
        "code_made_at": record["code_made_at"],
        "facts": found,
        "nudges": handover.nudges(record, fingerprint),
        # Both, so the plan page can say "no password or PIN yet" of somebody
        # the claim would turn away, and nothing of somebody who has a PIN.
        "people": [{"id": u.id, "name": u.display_name, "username": u.username,
                    "has_password": u.has_password, "has_pin": u.has_pin}
                   for u in _eligible(conn)],
        "claims": [_claim_out(c, names) for c in handover.claims(conn)],
    }


@admin_only.get("/api/admin/handover")
@require_admin
def handover_get():
    return jsonify(_page(_conn()))


@admin_only.post("/api/admin/handover")
@require_admin
def handover_save():
    """Keep the plan. Saving is reviewing: the nudge starts over from today."""
    data = json_object()
    conn = _conn()
    cfg = _cfg()
    services = _services()
    try:
        plan = handover.clean_plan(data.get("plan"),
                                   eligible=[u.id for u in _eligible(conn)],
                                   known_secrets=known_secrets(cfg, services))
        wait_days = handover.clean_wait_days(data.get("wait_days", handover.WAIT_DAYS_DEFAULT))
    except ValueError as exc:
        abort(400, description=str(exc))
    fingerprint = handover.destinations_fingerprint(destinations(cfg, services, conn))
    user = current_user()
    handover.save(conn, plan, wait_days=wait_days, user_id=user.id, destinations=fingerprint)
    auth.audit(conn, user.id, "handover_plan_saved",
               f"{len(plan['successors'])} successor(s), wait {wait_days} day(s)")
    return jsonify(_page(conn))


@admin_only.post("/api/admin/handover/code")
@require_admin
def handover_code():
    """A new handover code, shown this once. The one before stops working."""
    conn = _conn()
    user = current_user()
    code = handover.make_code()
    handover.set_code(conn, user.id, auth.hash_password(handover.normalise_code(code)))
    auth.audit(conn, user.id, "handover_code_made", "a new handover code; any earlier one ended")
    return jsonify({"code": code, "made_at": time.time()})


# ---------------------------------------------------------------------------
# The printed sheet
# ---------------------------------------------------------------------------

_LOCALE_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def translator(lang: str) -> Callable[..., str]:
    """``t(key, **params)`` for the sheet, from the same locale files the
    console reads, so the sheet and the screen say the same thing."""
    table: dict[str, str] = {}
    # Only a file named from this list is ever read, whatever was asked for.
    name = {code: f"{code}.json" for code in LANGUAGES if code != "en"}.get(lang)
    if name:
        path = LOCALES / name
        try:
            stamp = path.stat().st_mtime
            cached = _LOCALE_CACHE.get(lang)
            if cached is None or cached[0] != stamp:
                cached = (stamp, json.loads(path.read_text(encoding="utf-8")))
                _LOCALE_CACHE[lang] = cached
            table = cached[1]
        except (OSError, ValueError):
            table = {}

    def t(key: str, **params: Any) -> str:
        return filled(table.get(key) or key, params)
    return t


def _date(value: float) -> str:
    """Written the same way in both languages, and the same on every printer."""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d") if value else ""


SHEET_CSS = """
@page { size: A4; margin: 16mm 15mm; }
* { box-sizing: border-box; }
html { background: #fff; color: #111; }
body { margin: 0 auto; max-width: 180mm; padding: 16px;
       font: 11pt/1.45 "Noto Serif", "Noto Serif Tamil", Georgia, serif; }
h1 { font-size: 20pt; line-height: 1.2; margin: 0 0 4px; }
h2 { font-size: 13pt; margin: 18px 0 6px; padding-bottom: 3px; border-bottom: 1.5px solid #111;
     break-after: avoid; }
p, li { margin: 4px 0; }
.lead { color: #333; margin-bottom: 10px; }
.note { border: 1.5px solid #111; padding: 8px 10px; margin: 10px 0; }
.letter { white-space: pre-wrap; border-left: 3px solid #111; padding-left: 10px; }
.pre { white-space: pre-wrap; }
table { width: 100%; border-collapse: collapse; margin: 6px 0; break-inside: avoid; }
th, td { text-align: left; vertical-align: top; padding: 4px 6px; border-bottom: 1px solid #999; }
th { font-weight: 600; width: 34%; }
thead th { width: auto; border-bottom: 1.5px solid #111; }
code, .mono { font-family: "Noto Sans Mono", ui-monospace, Menlo, Consolas, monospace;
              font-size: 10pt; overflow-wrap: anywhere; }
.code { font-family: "Noto Sans Mono", ui-monospace, Menlo, Consolas, monospace;
        font-size: 18pt; letter-spacing: 0.08em; border: 2px solid #111; padding: 10px 14px;
        display: inline-block; margin: 6px 0; min-width: 120mm; min-height: 14mm; }
ol li { margin-bottom: 6px; }
section { break-inside: avoid-page; }
.foot { margin-top: 18px; color: #444; font-size: 9pt; }
.bar { display: flex; gap: 8px; justify-content: flex-end; margin-bottom: 8px; }
.bar button { font: inherit; padding: 6px 14px; border: 1.5px solid #111; background: #fff;
              border-radius: 6px; cursor: pointer; }
@media print { .bar { display: none; } body { padding: 0; max-width: none; } }
@media (max-width: 600px) { body { font-size: 10.5pt; } th { width: 40%; } .code { min-width: 0;
  font-size: 14pt; } }
"""


def render_sheet(conn: sqlite3.Connection, lang: str, code: str = "") -> str:
    """The handover sheet as a page to print, in *lang* (``en`` or ``ta``).

    Everything typed by the administrator and everything read from the disk
    is escaped: the plan is the administrator's text, but a folder name is
    whatever somebody once called a folder.
    """
    lang = lang if lang in LANGUAGES else "en"
    t = translator(lang)
    e = html.escape
    record = handover.load(conn)
    plan = record["plan"]
    found = facts(conn)
    names = _names(conn)
    people = {u.id: u for u in auth.list_users(conn)}
    author = names.get(int(record["reviewed_by"] or 0)) or current_user().display_name
    dash = "—"
    # The default name is the program's own, which is a Tamil word: written
    # in Tamil on a Tamil sheet, as it is everywhere else in Tamil.
    home = found["home"]
    if home == "Ninaivu":
        home = t(said("Ninaivu"))

    def text(value: str) -> str:
        return e(value) if value else dash

    def row(label: str, value: str) -> str:
        return f"<tr><th>{e(label)}</th><td>{value}</td></tr>"

    out: list[str] = []
    add = out.append
    add(f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{e(t(said("If something happens to me — our family photos")))}</title>'
        f"<style>{SHEET_CSS}</style></head><body>")
    add(f'<div class="bar"><button type="button" id="print-sheet">{e(t(said("Print")))}</button></div>')
    add(f'<h1>{e(t(said("If something happens to me — our family photos")))}</h1>')
    add('<p class="lead">' + e(t(said("Written by {name} for {home}. Last reviewed {date}."),
                                  name=author, home=home,
                                  date=_date(record["reviewed_at"]) or dash)) + "</p>")
    add('<p class="note">' + e(t(said("Keep this sheet with the family's important papers. It never holds a password or a key — only where they are kept."))) + "</p>")

    if plan["message"]:
        add(f'<section><h2>{e(t(said("A message for you")))}</h2>'
            f'<p class="letter">{e(plan["message"])}</p></section>')

    add(f'<section><h2>{e(t(said("Who takes over")))}</h2>')
    named = [people[s] for s in plan["successors"] if s in people]
    if named:
        add(f'<p>{e(t(said("In this order. Each of them has their own profile in Ninaivu.")))}</p><ol>')
        for person in named:
            add(f"<li>{e(person.display_name)} <span class=\"mono\">({e(person.username)})</span></li>")
        add("</ol>")
    else:
        add(f"<p>{e(t(said('Nobody is named yet.')))}</p>")
    add("</section>")

    add(f'<section><h2>{e(t(said("How to take over as administrator")))}</h2><ol>')
    add(f"<li>{e(t(said('Sign in to your own profile in the family app, open your profile (your picture at the top), and choose Take over as administrator.')))}</li>")
    add(f"<li>{e(t(said('Type the handover code below and your password.')))}</li>")
    days = int(record["wait_days"])
    if days:
        add("<li>" + e(t(said("Ninaivu then waits {days} days before making you an administrator. Every administrator is told, and any of them can stop it in that time."), days=days)) + "</li>")
    else:
        add(f"<li>{e(t(said('You become an administrator straight away.')))}</li>")
    add("</ol>")
    add(f"<p><strong>{e(t(said('Handover code')))}</strong></p>")
    add(f'<div class="code">{e(code)}</div>')
    if not code:
        add(f"<p>{e(t(said('Write the handover code here when it is made. It is shown only once.')))}</p>")
    add(f"<p>{e(t(said('This code works once. If it has been used or replaced, ask an administrator for a new one.')))}</p>")
    add("</section>")

    add(f'<section><h2>{e(t(said("This computer")))}</h2><table>')
    add(row(t(said("Computer name")), f'<span class="mono">{e(found["machine"])}</span>'))
    add(row(t(said("How to reach it")), f'<span class="pre">{text(plan["machine_access"])}</span>'))
    add(row(t(said("The machine and its disks")), f'<span class="pre">{text(plan["hardware"])}</span>'))
    remote = found["remote"]
    reached = remote["title"] or t(said("Nothing set up"))
    if remote["hostnames"]:
        reached += " · " + ", ".join(remote["hostnames"])
    add(row(t(said("Reached from outside the house with")), e(reached)))
    add(row(t(said("Ninaivu version")), e(found["version"])))
    add(row(t(said("Ninaivu’s own folder")), f'<span class="mono">{e(found["state_dir"])}</span>'))
    add(row(t(said("Library folders")), "<br>".join(f'<span class="mono">{e(p)}</span>'
                                              for p in found["libraries"]) or dash))
    add("</table></section>")

    add(f'<section><h2>{e(t(said("Where the backups are")))}</h2>')
    if plan["backups_where"]:
        add(f'<p class="pre">{e(plan["backups_where"])}</p>')
    add(f'<table><thead><tr><th>{e(t(said("Backup")))}</th><th>{e(t(said("Where")))}</th>'
        f'<th>{e(t(said("Last good copy")))}</th></tr></thead><tbody>')
    for place in found["destinations"]:
        add(f'<tr><td>{e(t(place["title"]))}</td><td class="mono">{text(place["where"])}</td>'
            f'<td>{e(_date(place["last_ok"]) or t(said("never")))}</td></tr>')
    add("</tbody></table></section>")

    add(f'<section><h2>{e(t(said("Keys and passwords")))}</h2><table>')
    fingerprint = found["key_fingerprint"]
    add(row(t(said("Backup encryption key fingerprint")),
            f'<span class="mono">{e(fingerprint)}</span>' if fingerprint
            else e(t(said("No backup encryption key has been made.")))))
    add(row(t(said("Where the backup key’s recovery file and passphrase are kept")),
            f'<span class="pre">{text(plan["secrets_where"])}</span>'))
    add(row(t(said("Where the computer’s password is kept")),
            f'<span class="pre">{text(plan["password_where"])}</span>'))
    add("</table>")
    add(f"<p>{e(t(said('The fingerprint only lets you check that a recovery file is the right one. It cannot open anything.')))}</p>")
    add("</section>")

    if plan["helpers"]:
        add(f'<section><h2>{e(t(said("Who can help")))}</h2><table><thead><tr>'
            f'<th>{e(t(said("Name")))}</th><th>{e(t(said("Phone")))}</th></tr></thead><tbody>')
        for helper in plan["helpers"]:
            add(f'<tr><td>{text(helper.get("name", ""))}</td>'
                f'<td class="mono">{text(helper.get("phone", ""))}</td></tr>')
        add("</tbody></table></section>")

    add(f'<section><h2>{e(t(said("To bring the photographs back")))}</h2><ol>')
    for step in found["restore_steps"]:
        add(f"<li>{e(t(step))}</li>")
    add("</ol>")
    add(f"<p>{e(t(said('The full steps are in the Ninaivu guide, under Backup and recovery and Moving to another computer.')))}</p>")
    add("</section>")

    add('<p class="foot">' + e(t(said("Printed {date} by Ninaivu {version}."),
                                  date=_date(time.time()), version=found["version"])) + "</p>")
    add('<script src="/static/js/handover-sheet.js"></script></body></html>')
    return "".join(out)


def _sheet_response(body: str) -> Response:
    response = Response(body, mimetype="text/html")
    # A sheet with the code on it is not to be kept by anything on the way.
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_only.get("/api/admin/handover/sheet")
@require_admin
def handover_sheet():
    """The sheet to print, with a box to write the code in by hand."""
    return _sheet_response(render_sheet(_conn(), request.args.get("lang", "en")))


@admin_only.post("/api/admin/handover/sheet")
@require_admin
def handover_sheet_with_code():
    """The sheet with the code just made printed on it.

    Sent as a form from the console the moment the code is shown, so the code
    is never in an address (history, logs). It is printed only if it is the
    current code, checked against the hash; anything else gets the sheet with
    the empty box.
    """
    conn = _conn()
    given = handover.normalise_code(request.form.get("code", ""))
    stored = handover.code_hash(conn)
    code = ""
    if given and stored and auth.verify_password(given, stored):
        code = "-".join(given[i:i + 4] for i in range(0, len(given), 4))
    return _sheet_response(render_sheet(conn, request.form.get("lang", "en"), code))


# ---------------------------------------------------------------------------
# Every administrator: a claim that is waiting
# ---------------------------------------------------------------------------

@bp.get("/api/handover/claims")
@require_admin
def handover_claims():
    conn = _conn()
    complete_due(conn)
    names = _names(conn)
    return jsonify({"waiting": [_claim_out(c, names) for c in handover.waiting(conn)]})


@bp.post("/api/handover/claims/<int:claim_id>/cancel")
@require_admin
def handover_cancel(claim_id: int):
    """Stop a claim. The code it used is gone too: a new one is made on purpose."""
    conn = _conn()
    claim = handover.get_claim(conn, claim_id)
    if claim is None:
        abort(404)
    user = current_user()
    if not handover.cancel(conn, claim_id, user.id):
        return jsonify({"error": "That handover is not waiting any more."}), 409
    who = auth.get_user(conn, int(claim["user_id"]))
    auth.audit(conn, user.id, "handover_cancelled",
               f"{who.username if who else claim['user_id']}: stopped before the wait was over")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# A successor
# ---------------------------------------------------------------------------

#: Tries at the code. The code itself is far beyond guessing; these keep a
#: successor's session, or somebody at their keyboard, from trying anyway,
#: and the account-wide ones (``*|``) survive a restart (accounts_api).
_TRIES_PER_ADDRESS = (5, 300.0)
_TRIES_PER_PERSON = (5, 1800.0)
_TRIES_EVERYWHERE = (20, 1800.0)


def _mine(conn: sqlite3.Connection, user: auth.User) -> dict[str, Any] | None:
    for claim in handover.claims(conn, limit=5):
        if int(claim["user_id"]) == user.id:
            return claim
    return None


@bp.get("/api/handover/me")
@require_family
def handover_me():
    """Whether this person is named as a successor, and how their claim is going.
    Somebody who is not named learns nothing about who is."""
    conn = _conn()
    complete_due(conn)
    user = current_user()
    named = user.id in handover.load(conn)["plan"]["successors"]
    claim = _mine(conn, user) if named else None
    return jsonify({
        "successor": named and not user.is_admin,
        "has_password": user.has_password,
        # What the claim will ask for besides the code: the password when
        # there is one, else the PIN the profile opens with — and a profile
        # with neither is told to get one of them first.
        "has_pin": user.has_pin,
        "claim": {"state": claim["state"], "requested_at": claim["requested_at"],
                  "due_at": claim["due_at"]} if claim else None,
    })


@bp.post("/api/handover/claim")
@require_family
def handover_claim():
    """A successor, signed in as themselves, asks to become an administrator.

    In this order, so that each refusal says as little as it can: this
    address's and person's allowance (counted before anything is checked),
    whether this person is named at all (a code found by anybody else is not
    even looked at), the household's allowance, something of their own
    (being signed in is not proof of who is at the keyboard), then the code.
    Every refusal is written to the activity log.

    "Something of their own" is the password when the profile has one, else
    the PIN it opens with. It used to be that a profile with no password
    chose one in the same breath as the claim, which proved nothing: whoever
    found the profile open on the family tablet, with the printed sheet,
    could type any password they liked. A profile with neither — tap to
    enter — has nothing to check and is turned away until an administrator
    gives it a PIN or a password; the plan page says so beside the name.
    """
    from .accounts_api import reauthenticate_limited, reserve, verify_pin_limited  # noqa: PLC0415
    data = json_object()
    conn = _conn()
    user = current_user()
    too_many = jsonify({"error": "Too many attempts. Wait a while and try again."}), 429
    if not reserve([(f"{request.remote_addr}|handover", *_TRIES_PER_ADDRESS),
                    (f"*|handover:{user.id}", *_TRIES_PER_PERSON)]):
        auth.audit(conn, user.id, "handover_limited", user.username)
        return too_many

    record = handover.load(conn)
    if user.is_admin or user.role != ROLE_FAMILY or user.id not in record["plan"]["successors"]:
        auth.audit(conn, user.id, "handover_refused",
                   f"{user.username}: not named as a successor")
        return jsonify({"error": "Only somebody named in the handover plan can use a "
                                 "handover code."}), 403
    # The household-wide allowance is spent only by those named: anybody else
    # spending it would lock the real successor out when they need it most.
    if not reserve([("*|handover", *_TRIES_EVERYWHERE)]):
        auth.audit(conn, user.id, "handover_limited", user.username)
        return too_many
    if any(int(c["user_id"]) == user.id for c in handover.waiting(conn)):
        return jsonify({"error": "Your handover is already waiting."}), 409

    password = str(data.get("password") or "")
    if user.has_password:
        answer = reauthenticate_limited(conn, user.id, password)
        if answer is None:
            return too_many
        if not answer:
            auth.audit(conn, user.id, "handover_refused", f"{user.username}: wrong password")
            return jsonify({"error": "Your password isn't right."}), 403
    elif user.has_pin:
        answer = verify_pin_limited(conn, user.id, str(data.get("pin") or ""))
        if answer is None:
            return too_many
        if not answer:
            auth.audit(conn, user.id, "handover_refused", f"{user.username}: wrong PIN")
            return jsonify({"error": "Your PIN isn't right."}), 403
        # An administrator signs in with a password, so one is chosen now
        # and kept only once the claim is made (below).
        if (problem := auth.password_problem(password)):
            return jsonify({"error": "An administrator signs in with a password. Choose one "
                                     f"for your profile here. {problem}"}), 400
    else:
        auth.audit(conn, user.id, "handover_refused",
                   f"{user.username}: the profile has neither a password nor a PIN")
        return jsonify({"error": "Your profile opens with a tap, so there is nothing of "
                                 "your own for Ninaivu to check. An administrator can give "
                                 "it a PIN or a password; then type the code again."}), 403

    given = handover.normalise_code(data.get("code"))
    stored = handover.code_hash(conn)
    if not given or not stored or not auth.verify_password(given, stored):
        auth.audit(conn, user.id, "handover_code_wrong", user.username)
        return jsonify({"error": "That handover code is not right, or it has been used or "
                                 "replaced."}), 403

    claim = handover.start_claim(conn, user.id, stored)
    if claim is None:
        auth.audit(conn, user.id, "handover_code_wrong", f"{user.username}: used or replaced")
        return jsonify({"error": "That handover code is not right, or it has been used or "
                                 "replaced."}), 403
    if not user.has_password:
        # Only now, with the claim made: a refused claim changes nothing. The
        # PIN proved who chose it, a moment ago.
        auth.set_password(conn, user.id, password)
    days = round((claim["due_at"] - claim["requested_at"]) / handover.DAY)
    auth.audit(conn, user.id, "handover_requested",
               f"{user.username}: becomes an administrator in {days} day(s) unless cancelled")
    made = complete_due(conn)
    return jsonify({"ok": True, "done": bool(made),
                    "claim": {"state": handover.get_claim(conn, claim["id"])["state"],
                              "requested_at": claim["requested_at"],
                              "due_at": claim["due_at"]}})
