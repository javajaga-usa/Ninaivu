"""Sending one photograph back to the household, once a week.

Everything else Ninaivu does is about keeping: indexing, archiving, backing up,
verifying. The measurements that prompted this are stark — a library of
196,174 photographs spanning 1980 to 2026, with 226,863 faces found and ten
people named by hand, in which four photographs have ever been favourited and
none has ever been collected into an album or shared. The library is written
to and not read from.

So this is the one part of Ninaivu that speaks first. Once a week it picks the
best photograph from this day in a past year (``db.pick_for_the_day``) and
sends it to whoever asked for it, with the picture in the message rather than
behind a link — a link to a home server is a link that works in the house and
nowhere else, and the point is the photograph, not the trip to go and see it.

**This does not follow the rules in ``notify.py``.** That module promises
never to send a photograph, a path or a name, and it is right to: it reports
that a drive is unplugged to whoever administers the machine. This is the
opposite thing — a photograph of your family, sent to your family, because
they asked for it. Two products, two promises, so this has its own settings
and its own sender rather than quietly widening that one.

What it borrows is the mail server. A household should not have to configure
SMTP twice.
"""

from __future__ import annotations

import logging
import smtplib
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ninaivu.digest")

SEND_TIMEOUT = 20.0

#: The size embedded in the message. The larger of the two thumbnails Ninaivu
#: already writes, so nothing has to be generated and nothing has to be read
#: from the library drive — which may not even be connected when this runs.
EMBED_SIZE = 640

#: Mail clients that render WebP are still the minority, and a photograph
#: that arrives as a broken image is worse than one that never arrives.
EMBED_FORMAT = "JPEG"
EMBED_QUALITY = 86

#: Monday is 0, as `datetime.weekday()` counts. Sunday by default: the
#: morning a household is most likely to have a minute for it.
DEFAULT_WEEKDAY = 6
DEFAULT_HOUR = 9

#: Never twice for the same day, however often the loop comes round or the
#: machine is restarted.
ALREADY_SENT_KEY = "digest:last_sent"


def thumbnail_bytes(cfg: Any, item: dict[str, Any]) -> bytes | None:
    """The photograph, as something a mail client will actually draw.

    None when the thumbnail is missing, which is not an error: the picker
    only offers items that have one recorded, but a file can go missing
    between the index and the disk.
    """
    from ..media import media                              # noqa: PLC0415

    base = item.get("thumb") or ""
    if not base:
        return None
    path = Path(cfg.thumbs_dir) / media.thumb_file(
        base, EMBED_SIZE, getattr(cfg, "thumb_format", "WEBP"))
    if not path.is_file():
        return None
    try:
        from PIL import Image                              # noqa: PLC0415

        with Image.open(path) as image:
            out = BytesIO()
            image.convert("RGB").save(
                out, EMBED_FORMAT, quality=EMBED_QUALITY, optimize=True)
            return out.getvalue()
    except Exception as exc:                               # noqa: BLE001
        log.debug("could not prepare %s for sending: %s", path, exc)
        return None


def describe(item: dict[str, Any], *, house: str = "Ninaivu") -> tuple[str, str]:
    """The subject line and the sentence under the photograph.

    Deliberately short and deliberately not clever. "Nine years ago today" is
    the whole idea; anything more is a caption written by something that was
    not there.
    """
    year = str(item.get("year") or "")
    now = int(time.strftime("%Y"))
    if year.isdigit():
        behind = now - int(year)
        when = ("A year ago today" if behind == 1
                else f"{behind} years ago today")
        subject = f"{house}: {when.lower()[0]}{when[1:]}, {year}"
    else:
        when, subject = "From the library", f"{house}: a photograph"

    faces = int(item.get("face_count") or 0)
    if faces > 1:
        line = f"{when} — {faces} of you."
    elif faces == 1:
        line = f"{when} — one of you."
    else:
        line = f"{when}."
    return subject, line


def compose(item: dict[str, Any], picture: bytes | None, *,
            house: str = "Ninaivu", sender: str = "", to: str = "",
            others: int = 0, link: str = "") -> EmailMessage:
    """One message, with the photograph in it rather than behind a link."""
    subject, line = describe(item, house=house)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to

    more = (f"\n\nThere are {others:,} more photographs from this day."
            if others else "")
    where = f"\n\n{link}" if link else ""
    message.set_content(f"{line}{more}{where}\n")

    if picture is None:
        return message

    cid = make_msgid()
    trimmed = cid[1:-1]
    body = [
        '<div style="font-family:system-ui,-apple-system,sans-serif;'
        'max-width:640px">',
        f'<img src="cid:{trimmed}" alt="" '
        'style="width:100%;height:auto;border-radius:10px">',
        f'<p style="font-size:15px;color:#333">{line}</p>',
    ]
    if others:
        body.append('<p style="font-size:13px;color:#777">'
                    f'There are {others:,} more photographs from this day.</p>')
    if link:
        body.append(f'<p style="font-size:13px"><a href="{link}">'
                    'See the rest</a></p>')
    body.append("</div>")
    message.add_alternative("".join(body), subtype="html")
    message.get_payload()[1].add_related(
        picture, "image", EMBED_FORMAT.lower(), cid=cid)
    return message


@dataclass
class Digest:
    """Picks the photograph and sends it. Holds nothing worth persisting."""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    to: str = ""
    house: str = "Ninaivu"
    link: str = ""
    #: Overridable so tests never touch a socket. Given the composed message.
    transport: Callable[[EmailMessage], None] | None = field(
        default=None, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.smtp_host and self.to)

    def send(self, message: EmailMessage) -> str:
        if self.transport is not None:
            try:
                self.transport(message)
                return "ok"
            except Exception as exc:                       # noqa: BLE001
                return str(exc)[:200]
        try:
            # The same connection rules as the notifications (verified TLS,
            # and never a password over a plain connection): one place.
            from .notify import send_mail                  # noqa: PLC0415
            send_mail(message, host=self.smtp_host, port=self.smtp_port,
                      user=self.smtp_user, password=self.smtp_password,
                      tls=self.smtp_tls, timeout=SEND_TIMEOUT)
            return "ok"
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            log.warning("the digest could not be sent: %s", exc)
            return str(exc)[:200]


def from_config(cfg: Any) -> Digest:
    """The mail server from the notification settings, the rest its own.

    Borrowing the server and not the recipient is the point: alerts go to
    whoever looks after the machine, and photographs go to the family.
    """
    from ..server.config import house_name                 # noqa: PLC0415

    return Digest(
        smtp_host=getattr(cfg, "notify_smtp_host", "") or "",
        smtp_port=int(getattr(cfg, "notify_smtp_port", 587) or 587),
        smtp_user=getattr(cfg, "notify_smtp_user", "") or "",
        smtp_password=getattr(cfg, "notify_smtp_password", "") or "",
        smtp_tls=bool(getattr(cfg, "notify_smtp_tls", True)),
        to=getattr(cfg, "digest_to", "") or "",
        house=house_name(cfg),
        link=getattr(cfg, "digest_link", "") or "",
    )


def build(cfg: Any, conn: Any, roots: Any, *, month: int | None = None,
          day: int | None = None) -> dict[str, Any]:
    """Pick today's photograph and compose the message. Sends nothing.

    Separate from sending so the console can show what would go out, and so a
    test can read the message without a mail server.
    """
    from ..storage import db                               # noqa: PLC0415

    when = time.localtime()
    month = when.tm_mon if month is None else month
    day = when.tm_mday if day is None else day

    candidates = db.candidates_for_the_day(conn, roots, month=month, day=day)
    if not candidates:
        return {"ready": False, "reason": "nothing worth sending from this day",
                "month": month, "day": day}

    item = candidates[0]
    digest = from_config(cfg)
    picture = thumbnail_bytes(cfg, item)
    message = compose(
        item, picture, house=digest.house,
        sender=digest.smtp_user or f"ninaivu@{digest.smtp_host or 'localhost'}",
        to=digest.to, others=len(candidates) - 1, link=digest.link)
    return {
        "ready": True, "month": month, "day": day,
        "item": item, "others": len(candidates) - 1,
        "message": message, "has_picture": picture is not None,
        "subject": message["Subject"],
    }


class DigestKeeper:
    """Sends the weekly photograph, quietly, on a schedule.

    The same shape as ``BackupKeeper``: a thread that wakes, asks whether
    anything is due, and goes back to sleep. Never more than one at a time,
    and never twice for the same day — a restart on a Sunday afternoon must
    not send a second one.
    """

    def __init__(self, cfg: Any, connect: Callable[[], Any],
                 roots: Callable[[], Any]) -> None:
        self.cfg = cfg
        self._connect = connect
        self._roots = roots
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = threading.Lock()
        self.last_error: str | None = None

    # -- when --------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "digest_enabled", False)
                    and from_config(self.cfg).configured)

    @property
    def busy(self) -> bool:
        return self._running.locked()

    def due(self, now: time.struct_time | None = None) -> bool:
        """Is this the morning, and has this day's not gone already?"""
        if not self.enabled:
            return False
        now = now or time.localtime()
        weekday = int(getattr(self.cfg, "digest_weekday", DEFAULT_WEEKDAY))
        hour = int(getattr(self.cfg, "digest_hour", DEFAULT_HOUR))
        if now.tm_wday != weekday or now.tm_hour < hour:
            return False
        return self._last_sent() != time.strftime("%Y-%m-%d", now)

    def _last_sent(self) -> str:
        from ..storage import db                           # noqa: PLC0415

        try:
            return db.get_meta(self._connect(), ALREADY_SENT_KEY) or ""
        except Exception:                                  # noqa: BLE001
            return ""

    def _remember(self, day: str) -> None:
        from ..storage import db                           # noqa: PLC0415

        try:
            conn = self._connect()
            db.set_meta(conn, ALREADY_SENT_KEY, day)
            conn.commit()
        except Exception:                                  # noqa: BLE001
            log.debug("could not write down that the digest went")

    # -- the loop ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ninaivu-digest",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        # Every twenty minutes is often enough for something that happens
        # once a week, and cheap enough that nobody notices it.
        while not self._stop.wait(1200):
            try:
                if self.due():
                    self.run()
            except Exception as exc:                       # noqa: BLE001
                self.last_error = str(exc)
                log.exception("the weekly photograph could not be sent")

    def run(self, *, force: bool = False,
            when: time.struct_time | None = None) -> dict[str, Any]:
        """Send one now. Safe from a route or from the loop.

        *force* is the console's "send me one now": it ignores the day and
        the hour, but still refuses to invent a photograph on a day that has
        none.

        *when* names the day to send for, the same way :meth:`due` takes one.
        A weekly thing whose behaviour can only be observed on the right
        morning is a weekly thing nobody can test.
        """
        when = when or time.localtime()
        if not self._running.acquire(blocking=False):
            return {"sent": False, "reason": "one is already going out"}
        try:
            self.last_error = None
            digest = from_config(self.cfg)
            if not digest.configured:
                return {"sent": False, "reason": "no mail server or nobody to send to"}
            built = build(self.cfg, self._connect(), self._roots(),
                          month=when.tm_mon, day=when.tm_mday)
            if not built["ready"]:
                # A quiet week is a feature. Days with nothing worth sending
                # are what keep the ones that do arrive worth opening.
                log.info("no photograph worth sending today")
                return {"sent": False, "reason": built["reason"]}

            outcome = digest.send(built["message"])
            if outcome != "ok":
                self.last_error = outcome
                return {"sent": False, "reason": outcome}
            self._remember(time.strftime("%Y-%m-%d", when))
            log.info("sent the weekly photograph: %s", built["subject"])
            return {"sent": True, "subject": built["subject"],
                    "others": built["others"], "forced": bool(force)}
        finally:
            self._running.release()
