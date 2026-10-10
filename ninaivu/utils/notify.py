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

import http.client
import json
import logging
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable
from urllib.parse import urlsplit

from . import netscope
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

#: After a notice that reached nobody, it is tried again after each of these
#: (seconds), then left for whatever reports the same thing next.
RETRY_DELAYS = (60.0, 5 * 60.0, 15 * 60.0, 60 * 60.0)


def _later(delay: float, work: Callable[[], Any]) -> Any:
    timer = threading.Timer(delay, work)
    timer.daemon = True
    timer.start()
    return timer


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A webhook is posted to where it says. Following a redirect would let a
    saved address send Ninaivu on to anywhere, ftp:// included."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


class _Unroutable(ValueError):
    """The webhook names an address nothing should be posted to."""


def _resolve(host: str, port: int) -> list[tuple[int, tuple]]:
    """Where *host* is right now: ``(family, sockaddr)`` pairs, in the order
    the resolver gave them. Raises ``_Unroutable`` when any of them is an
    address that must never be a target (``netscope``), ``OSError`` when the
    name does not resolve.

    169.254.x.x and fe80:: are a cloud machine's own metadata service and a
    router's set-up pages, never a notification service. The house's own ntfy
    on the LAN stays allowed, and so does anything on the internet.
    """
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = []
    for family, _, _, _, sockaddr in found:
        ip = netscope.parse_ip(str(sockaddr[0]))
        if ip is None or netscope.never_a_target(ip):
            raise _Unroutable("The webhook cannot be a link-local address.")
        addresses.append((family, sockaddr))
    if not addresses:
        raise OSError("no address")
    return addresses


class _PinnedConnection(http.client.HTTPConnection):
    """A connection to the address that was checked, not to whatever the name
    resolves to a moment later. A name can be made to answer with a harmless
    address for the check and a link-local one for the connection; connecting
    to the checked address, with no second lookup, closes that gap. The name
    still goes in the ``Host`` header, and for https (below) is what the
    certificate is checked against."""

    address: tuple[int, tuple] = (socket.AF_INET, ("127.0.0.1", 0))

    def connect(self) -> None:
        family, sockaddr = self.address
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if isinstance(self.timeout, (int, float)):
                sock.settimeout(self.timeout)
            sock.connect(sockaddr)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _PinnedHTTPSConnection(http.client.HTTPSConnection, _PinnedConnection):
    """``HTTPSConnection.connect`` opens the socket through ``super()``, which
    the order of bases makes the pinned one, then wraps it for ``self.host`` —
    the name, so SNI and the certificate check are for the name typed."""


class _PinnedHandler(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
    def __init__(self, family: int, sockaddr: tuple) -> None:
        super().__init__()
        pinned = {"address": (family, sockaddr)}
        self._http = type("_Pinned", (_PinnedConnection,), pinned)
        self._https = type("_PinnedTLS", (_PinnedHTTPSConnection,), pinned)

    def http_open(self, req):  # noqa: ANN001
        return self.do_open(self._http, req)

    def https_open(self, req):  # noqa: ANN001
        return self.do_open(self._https, req, context=self._context)


class _Opener:
    """Posts a request to where its host name resolved when it was checked.

    One lookup, then a connection to what it returned, and no proxy in
    between: a proxy would make its own lookup, and the check here would then
    say nothing about where the post went.
    """

    def open(self, request: urllib.request.Request, timeout: float):  # noqa: ANN201
        parts = urlsplit(request.full_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = _resolve(parts.hostname or "", port)
        for n, (family, sockaddr) in enumerate(addresses, 1):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirects, _PinnedHandler(family, sockaddr))
            try:
                return opener.open(request, timeout=timeout)
            except urllib.error.URLError as exc:
                # On to the next address the resolver gave, as urllib would
                # have done itself had it been left to connect: a name whose
                # first answer is IPv6 (ntfy.sh's is) must still be reached
                # from a house with no IPv6 route. Every address was checked
                # above. An answer from the far end — HTTPError is a URLError
                # too — is final, and so is the last address failing.
                if (isinstance(exc, urllib.error.HTTPError) or n == len(addresses)
                        or not isinstance(exc.reason, OSError)):
                    raise
        raise OSError("no address")                      # _resolve never returns none


#: Replaceable in tests, which hand it something that records the request.
_OPENER: Any = _Opener()

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
    #: Events being sent right now, so two reports of one thing do not both go.
    _sending: set[str] = field(default_factory=set, repr=False)
    #: Failed attempts in a row, and the retry waiting, per event.
    _failures: dict[str, int] = field(default_factory=dict, repr=False)
    _retries: dict[str, Any] = field(default_factory=dict, repr=False)
    #: Set when new settings replace this notifier (see from_config), so a
    #: retry still waiting does not go to the old address with the old password.
    _retired: bool = field(default=False, repr=False)
    #: Overridable so tests never touch a socket.
    transport: Callable[[str, str, str], None] | None = field(default=None, repr=False)
    #: Runs ``work`` after ``delay`` seconds; overridable so tests need not wait.
    later: Callable[[float, Callable[[], Any]], Any] = field(default=_later, repr=False)

    # -- configuration ----------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.webhook_url or (self.smtp_host and self.smtp_to))

    def wants(self, event: str) -> bool:
        return event in EVENTS and event in self.enabled_events

    # -- sending ----------------------------------------------------------
    def send(self, event: str, summary: str, detail: str = "",
             *, force: bool = False, _retry: bool = False) -> dict[str, Any]:
        """Report one thing. Returns what was attempted, never raises.

        Callers are usually background threads doing something more important,
        so nothing here is allowed to escape: a notifier that throws would turn
        "the backup stalled" into "the backup crashed".
        """
        if not self.configured:
            return {"sent": False, "reason": "not configured"}
        # A forced send is the console's test, which asks whether the webhook
        # and the mail server work, not whether this event is ticked: with it
        # unticked the test used to answer "event not enabled".
        if not force and not self.wants(event):
            return {"sent": False, "reason": "event not enabled"}

        now = time.time()
        with self._guard:
            last = self._last_sent.get(event, 0.0)
            if not force and now - last < self.quiet_seconds:
                return {"sent": False, "reason": "already reported recently",
                        "next_in": int(self.quiet_seconds - (now - last))}
            # The quiet window starts only once a notice has reached somebody.
            # It used to start before sending, so a mail server or webhook
            # that was down for a minute kept the warning (a failing drive, a
            # stalled backup) quiet for six hours. While one is being sent,
            # another report of the same thing waits for its outcome instead.
            # A forced send is the console's test message, and a test is not
            # a report: the real alert that follows it must still go out.
            if not force:
                if event in self._sending:
                    return {"sent": False, "reason": "being sent now"}
                self._sending.add(event)
                # A new report, not one of the retries below, gets retries of
                # its own: after one long outage used them up, every later
                # report was tried once and then dropped.
                if not _retry and event not in self._retries:
                    self._failures.pop(event, None)
        try:
            results = self._deliver(summary, detail, event)
        finally:
            if not force:
                with self._guard:
                    self._sending.discard(event)
        if not force:
            self._settle(event, summary, detail, bool(results["sent"]))
        return results

    def _settle(self, event: str, summary: str, detail: str, sent: bool) -> None:
        """Start the quiet window after a delivery; after a failure, try again
        a little later, a few times, rather than wait for the next report."""
        with self._guard:
            if sent:
                self._last_sent[event] = time.time()
                self._failures.pop(event, None)
                retry = self._retries.pop(event, None)
                if retry is not None and hasattr(retry, "cancel"):
                    retry.cancel()
                return
            failures = self._failures.get(event, 0) + 1
            self._failures[event] = failures
            if event in self._retries or failures > len(RETRY_DELAYS):
                return
            delay = RETRY_DELAYS[failures - 1]

            def again() -> None:
                with self._guard:
                    self._retries.pop(event, None)
                    if self._retired:
                        return
                self.send(event, summary, detail, _retry=True)

            self._retries[event] = self.later(delay, again)
        log.info("notice %r reached nobody; trying again in %.0f s", event, delay)

    def _deliver(self, summary: str, detail: str, event: str) -> dict[str, Any]:
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
            elif self.webhook_format == "form":
                # Offered on the Advanced page, and until now sent as JSON
                # anyway, which an endpoint expecting a form turns down.
                from urllib.parse import urlencode                  # noqa: PLC0415
                body = urlencode({"title": title, "message": detail,
                                  "event": event, "source": "ninaivu"}).encode("utf-8")
                request = urllib.request.Request(
                    self.webhook_url, data=body, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
            else:
                payload = {"title": title, "message": detail,
                           "event": event, "source": "ninaivu",
                           # Slack and Discord both read "text"/"content".
                           # A real line break: "\\n" put a backslash and an
                           # "n" between the two on screen.
                           "text": f"{title}\n{detail}".strip(),
                           "content": f"{title}\n{detail}".strip()}
                request = urllib.request.Request(
                    self.webhook_url, data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
            # The opener resolves the address, refuses one that is nowhere
            # (see _resolve) and connects to the one it checked.
            with _OPENER.open(request, timeout=SEND_TIMEOUT):
                return "ok"
        except _Unroutable as exc:
            return str(exc)
        except urllib.error.HTTPError as exc:
            # Only the status: the console's "send a test" button showed the
            # first words of whatever answered, which made it a way to read
            # pages on the house's network.
            log.debug("webhook failed: %s", exc)
            return f"The webhook answered HTTP {exc.code}."
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("webhook failed: %s", exc)
            return "Could not reach the webhook."

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
            send_mail(message, host=self.smtp_host, port=self.smtp_port,
                      user=self.smtp_user, password=self.smtp_password,
                      tls=self.smtp_tls)
            return "ok"
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            log.debug("email failed: %s", exc)
            return str(exc)[:200]

    def retire(self) -> None:
        """Stop the retries this notifier still has waiting. Its settings were
        replaced; a webhook the administrator removed must not be posted to."""
        with self._guard:
            self._retired = True
            retries = list(self._retries.values())
            self._retries.clear()
        for retry in retries:
            if hasattr(retry, "cancel"):
                retry.cancel()

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


#: The port that speaks TLS from the first byte ("SMTPS"), rather than
#: starting in the clear and switching with STARTTLS.
SMTPS_PORT = 465


def _loopback(host: str) -> bool:
    """Whether *host* is this computer: a relay on it is reached without a wire."""
    import ipaddress                                       # noqa: PLC0415

    name = (host or "").strip().strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def send_mail(message: EmailMessage, *, host: str, port: int, user: str = "",
              password: str = "", tls: bool = True, timeout: float = SEND_TIMEOUT) -> None:
    """Hand *message* to the mail server. The notifications and the weekly
    photograph both send through here, so the rules are the same for both.

    Port 465 is TLS from the start; any other port is upgraded with STARTTLS
    when ``tls`` is on. Either way the connection is verified: an unchecked
    one hands the password, and the weekly photograph, to anybody who can sit
    between here and the mail server. With TLS off, the password is sent only
    to a mail relay on this computer — never across the network in the clear.
    Raises ``ValueError`` (with words for the console) rather than do that.
    """
    secure = tls or int(port) == SMTPS_PORT
    if user and not secure and not _loopback(host):
        reason = ("the mail password would be sent unencrypted; switch on TLS for the "
                  "mail server (or use port 465)")
        log.warning("email not sent to %s: %s", host, reason)
        raise ValueError(reason)
    if int(port) == SMTPS_PORT:
        connection = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                      context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(host, port, timeout=timeout)
    with connection as server:
        if tls and int(port) != SMTPS_PORT:
            server.starttls(context=ssl.create_default_context())
        if user:
            server.login(user, password)
        server.send_message(message)


#: The one notifier for this process, with the settings it was built from.
#: The quiet window lives on the instance, so a caller that built a fresh
#: notifier for every message (the services did, for years) never had it: a
#: drive left unplugged was reported every hour for as long as it was away.
_shared: tuple[dict[str, Any], Notifier] | None = None
_shared_guard = threading.Lock()


def _settings(cfg: Any) -> dict[str, Any]:
    from ..server.config import house_name  # noqa: PLC0415 - avoids an import cycle

    events = getattr(cfg, "notify_events", None) or list(EVENTS)
    return {
        "webhook_url": getattr(cfg, "notify_webhook", "") or "",
        "webhook_format": getattr(cfg, "notify_webhook_format", "json") or "json",
        "smtp_host": getattr(cfg, "notify_smtp_host", "") or "",
        "smtp_port": int(getattr(cfg, "notify_smtp_port", 587) or 587),
        "smtp_user": getattr(cfg, "notify_smtp_user", "") or "",
        "smtp_password": getattr(cfg, "notify_smtp_password", "") or "",
        "smtp_to": getattr(cfg, "notify_smtp_to", "") or "",
        "smtp_tls": bool(getattr(cfg, "notify_smtp_tls", True)),
        "enabled_events": tuple(events),
        "quiet_seconds": float(getattr(cfg, "notify_quiet_seconds", DEFAULT_QUIET_SECONDS)),
        "house": house_name(cfg),
    }


def from_config(cfg: Any) -> Notifier:
    """The notifier for these settings: the same one while they are unchanged.

    A settings change gives a new notifier, but what the old one has already
    said comes with it. Changing the webhook address must not re-send every
    problem of the last six hours.
    """
    global _shared
    settings = _settings(cfg)
    with _shared_guard:
        if _shared is not None and _shared[0] == settings:
            return _shared[1]
        notifier = Notifier(**settings)
        if _shared is not None:
            old = _shared[1]
            notifier._last_sent = dict(old._last_sent)          # noqa: SLF001
            old.retire()
        _shared = (settings, notifier)
        return notifier
