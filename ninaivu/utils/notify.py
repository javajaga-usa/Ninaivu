"""Telling somebody when the quiet things go wrong.

A media server that runs all year fails silently by nature. The library sits
there looking healthy while the cloud backup has not run since March, the
archive is waiting on a drive somebody unplugged in June, and a scrubber pass
found a photograph whose bytes have changed. Nobody is watching the console at
the moment any of that happens, which is precisely why it goes unnoticed.

So Ninaivu can send a line somewhere. Deliberately small:

* Off unless configured. No default endpoint, nothing phoning anywhere.
* Only things a person would act on. Not "a scan finished" — that is every
  night, and a notification that arrives every night is one nobody reads.
* Never the photographs, never a path inside the library, never a name. A
  notification says what happened and how many; it does not describe what is
  in the pictures.
* A failure to notify is never a failure of the thing being reported. If the
  webhook is down, the scrubber has still done its work.

Two transports, because between them they cover almost everybody: an HTTP POST
(which is what ntfy, Gotify, Discord, Slack and Home Assistant all accept) and
plain SMTP.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable

from ..words import said

log = logging.getLogger("ninaivu.notify")

#: What can be reported, and what each one means. The keys are stable; the
#: console shows the descriptions.
EVENTS: dict[str, str] = {
    "integrity": said("A file's contents changed on disk without being edited"),
    "missing": said("Indexed files are no longer where they were"),
    "cloud_stalled": said("Cloud backup has not made progress for days"),
    "cloud_failed": said("Cloud backup stopped with an error"),
    "archive_waiting": said("The archive is waiting for a disconnected drive"),
    "scan_errors": said("A library scan finished with errors"),
    "disk_low": said("The drive holding the archive is nearly full"),
    "disk_health": said("A drive Ninaivu uses is logging errors and may be failing"),
    "restore_test": said("A test restore from the cloud backup did not come back intact"),
    "cloud_approval": said("Large files are waiting for approval before the cloud backup"),
}

#: Never send the same thing twice within this window. A drive left unplugged
#: over a weekend should produce one message, not one every poll.
DEFAULT_QUIET_SECONDS = 6 * 60 * 60

SEND_TIMEOUT = 15.0


@dataclass
class Notifier:
    """Sends the lines. Holds no state worth persisting beyond what it has said."""

    webhook_url: str = ""
    webhook_format: str = "json"      # json | ntfy | form
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_to: str = ""
    smtp_tls: bool = True
    enabled_events: tuple[str, ...] = tuple(EVENTS)
    quiet_seconds: float = DEFAULT_QUIET_SECONDS
    house: str = "Ninaivu"

    _last_sent: dict[str, float] = field(default_factory=dict, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: Overridable so tests never touch a socket.
    transport: Callable[[str, str, str], None] | None = field(default=None, repr=False)

    # -- configuration ----------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.webhook_url or (self.smtp_host and self.smtp_to))

    def wants(self, event: str) -> bool:
        return event in EVENTS and event in self.enabled_events

    # -- sending ----------------------------------------------------------
    def send(self, event: str, summary: str, detail: str = "",
             *, force: bool = False) -> dict[str, Any]:
        """Report one thing. Returns what was attempted, never raises.

        Callers are usually background threads doing something more important,
        so nothing here is allowed to escape: a notifier that throws would turn
        "the backup stalled" into "the backup crashed".
        """
        if not self.configured:
            return {"sent": False, "reason": "not configured"}
        if not self.wants(event):
            return {"sent": False, "reason": "event not enabled"}

        now = time.time()
        with self._guard:
            last = self._last_sent.get(event, 0.0)
            if not force and now - last < self.quiet_seconds:
                return {"sent": False, "reason": "already reported recently",
                        "next_in": int(self.quiet_seconds - (now - last))}
            self._last_sent[event] = now

        title = f"{self.house}: {summary}"
        results = {"sent": False, "webhook": None, "email": None}
        if self.webhook_url:
            results["webhook"] = self._post(title, detail, event)
            results["sent"] = results["sent"] or results["webhook"] == "ok"
        if self.smtp_host and self.smtp_to:
            results["email"] = self._email(title, detail)
            results["sent"] = results["sent"] or results["email"] == "ok"
        return results

    def _post(self, title: str, detail: str, event: str) -> str:
        from urllib.parse import urlsplit                       # noqa: PLC0415
        if not self.transport and urlsplit(self.webhook_url).scheme not in ("http", "https"):
            # urllib would also open file:// and ftp:// — and report what it
            # found back through the console's "send a test" button.
            return "The webhook must be an http:// or https:// address."
        if self.transport:
            try:
                self.transport("webhook", title, detail)
                return "ok"
            except Exception as exc:  # noqa: BLE001
                return str(exc)[:200]
        try:
            if self.webhook_format == "ntfy":
                body = detail.encode("utf-8") or title.encode("utf-8")
                headers = {"Title": title, "Tags": "warning"}
                request = urllib.request.Request(
                    self.webhook_url, data=body, headers=headers, method="POST")
            else:
                payload = {"title": title, "message": detail,
                           "event": event, "source": "ninaivu",
                           # Slack and Discord both read "text"/"content".
                           "text": f"{title}\\n{detail}".strip(),
                           "content": f"{title}\\n{detail}".strip()}
                request = urllib.request.Request(
                    self.webhook_url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=SEND_TIMEOUT):
                return "ok"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("webhook failed: %s", exc)
            return str(exc)[:200]

    def _email(self, title: str, detail: str) -> str:
        if self.transport:
            try:
                self.transport("email", title, detail)
                return "ok"
            except Exception as exc:  # noqa: BLE001
                return str(exc)[:200]
        try:
            message = EmailMessage()
            message["Subject"] = title
            message["From"] = self.smtp_user or f"ninaivu@{self.smtp_host}"
            message["To"] = self.smtp_to
            message.set_content(detail or title)
            with smtplib.SMTP(self.smtp_host, self.smtp_port,
                              timeout=SEND_TIMEOUT) as server:
                if self.smtp_tls:
                    # A verified connection: an unchecked one hands the
                    # password, and the weekly photograph, to anybody
                    # who can sit between here and the mail server.
                    server.starttls(context=ssl.create_default_context())
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_password)
                server.send_message(message)
            return "ok"
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            log.debug("email failed: %s", exc)
            return str(exc)[:200]

    # -- for the console --------------------------------------------------
    def test(self) -> dict[str, Any]:
        """Prove it works, ignoring the quiet window."""
        return self.send("integrity",
                         "Test notification",
                         "If you are reading this, notifications are working.",
                         force=True)

    def snapshot(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "webhook": bool(self.webhook_url),
            "webhook_format": self.webhook_format,
            "email": bool(self.smtp_host and self.smtp_to),
            "events": list(self.enabled_events),
            "known_events": EVENTS,
            "quiet_hours": round(self.quiet_seconds / 3600, 1),
        }


def from_config(cfg: Any) -> Notifier:
    from ..server.config import house_name  # noqa: PLC0415 - avoids an import cycle

    events = getattr(cfg, "notify_events", None) or list(EVENTS)
    return Notifier(
        webhook_url=getattr(cfg, "notify_webhook", "") or "",
        webhook_format=getattr(cfg, "notify_webhook_format", "json") or "json",
        smtp_host=getattr(cfg, "notify_smtp_host", "") or "",
        smtp_port=int(getattr(cfg, "notify_smtp_port", 587) or 587),
        smtp_user=getattr(cfg, "notify_smtp_user", "") or "",
        smtp_password=getattr(cfg, "notify_smtp_password", "") or "",
        smtp_to=getattr(cfg, "notify_smtp_to", "") or "",
        smtp_tls=bool(getattr(cfg, "notify_smtp_tls", True)),
        enabled_events=tuple(events),
        quiet_seconds=float(getattr(cfg, "notify_quiet_seconds",
                                    DEFAULT_QUIET_SECONDS)),
        house=house_name(cfg),
    )
