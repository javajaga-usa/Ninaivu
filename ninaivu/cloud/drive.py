"""Google Drive: signing in, staying signed in, and putting files up.

Two things about this module are worth knowing before reading it.

**Ninaivu does not ship a Google client secret, and cannot.** An OAuth client
secret embedded in an application anybody can unzip is not a secret; Google
say so themselves, and a shared one would put every Ninaivu household behind one
quota and one revocation. So the administrator makes their own — a Google Cloud
project, the Drive API switched on, an OAuth client of type *Web application*
with the console's own callback as an authorised redirect URI — and pastes the
two values in once. It is five minutes and it cannot be avoided, so the console
says so, and shows the exact redirect URI to paste, rather than pretending
otherwise.

*Web application* rather than *Desktop app* because the console is routinely
reached from another machine in the house: a desktop client only permits
``localhost`` redirects, which would work for the computer Ninaivu runs on and
nowhere else.

**Everything network-facing goes through one function.** :func:`_request` is
the only place this module touches HTTP, and it can be replaced. That is what
lets the whole of this — the consent exchange, token refresh, resumable
uploads, Google's own error codes and retry advice — be exercised against a
stand-in that behaves like Drive, rather than being hoped about.

The scope asked for is ``drive.file``: permission to see and manage *only the
files this application creates*. Not the user's existing Drive, not their
documents, not anything they made elsewhere. It is the narrowest scope that
allows an upload, and it means a Ninaivu token cannot read a single thing the
household did not put there through Ninaivu.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import http.client
import mimetypes
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = [
    "SCOPE", "AUTH_URL", "TOKEN_URL", "DriveError", "NeedsReconnect",
    "Credentials", "DriveClient", "consent_url", "CHUNK",
    "redirect_uri_problem", "LOOPBACK_HOSTS",
]

#: Hosts Google treats as this computer, and allows with either scheme.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

#: Endings Google refuses in a redirect URI: a name only this house can
#: resolve is not a domain it will send a browser to.
PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".intranet",
                    ".private", ".test", ".example", ".invalid", ".localdomain")


def redirect_uri_problem(uri: str) -> str | None:
    """Why Google will not accept this redirect URI, or ``None`` if it will.

    Google checks the URI against its own rules before it checks it against
    the client's list, and a home server fails those rules in two ways that
    look identical from the browser (``redirect_uri_mismatch``): a name like
    ``ninaivu-admin.local``, which nothing outside the house can resolve, and
    a private address like ``192.168.0.119``. Neither can even be saved in
    the Google Cloud console, so no amount of pasting makes them match.

    The way through is the loopback name, which Google allows for exactly
    this case: open the console on the computer Ninaivu runs on.
    """
    parsed = urllib.parse.urlsplit(uri or "")
    host = (parsed.hostname or "").lower()
    if not host:
        return "Ninaivu could not work out the address this console was opened on."
    loopback = host in ("localhost", "127.0.0.1", "::1")
    if loopback:
        return None
    if parsed.scheme != "https":
        return ("Google only accepts an address beginning https:// unless it is "
                "localhost.")
    if any(host.endswith(suffix) for suffix in PRIVATE_SUFFIXES):
        return (f"Google will not send a browser to \u201c{host}\u201d: a name only "
                f"this house can resolve cannot be an authorised redirect URI.")
    if host.replace(".", "").isdigit() or ":" in host:
        return (f"Google will not accept the address \u201c{host}\u201d: it wants a "
                f"domain name, not an IP address.")
    if "." not in host:
        return (f"Google will not accept \u201c{host}\u201d: it wants a full domain "
                f"name.")
    return None

#: See the module docstring: files this application created, and nothing else.
SCOPE = "https://www.googleapis.com/auth/drive.file"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
API_URL = "https://www.googleapis.com/drive/v3/files"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

#: Bytes per chunk of a resumable upload. Google requires a multiple of 256 KiB
#: for every chunk but the last. Thirty-two megabytes keeps the number of round
#: trips low — a 1 GB video is 32 chunks rather than 128 — while still costing
#: only seconds to re-upload if a chunk fails. The existing retry and resume
#: logic handles partial failures, and the 300 s send timeout is generous.
CHUNK = 32 * 1024 * 1024

#: Google's own advice for these is to back off and try again; anything else is
#: a real answer and is not worth retrying.
RETRY_STATUS = {429, 500, 502, 503, 504}

#: The reasons Google gives, with a 403 rather than a 429, for "slow down".
#: Drive's documentation says to back off exponentially on these exactly as on
#: a 429; read as a plain refusal, an afternoon of them failed every file that
#: happened to be in flight. Compared with case and underscores ignored, so the
#: older ``errors[].reason`` spelling and the newer ``RATE_LIMIT_EXCEEDED`` in
#: ``details`` both count.
RATE_LIMIT_REASONS = frozenset({"ratelimitexceeded", "userratelimitexceeded"})

#: 403 reasons that are about the whole account rather than the file: Drive is
#: full, or the day's allowance is spent. Not worth retrying in a minute, and
#: not the fault of whichever file happened to be going up.
ACCOUNT_REASONS = frozenset({"storagequotaexceeded", "quotaexceeded",
                             "dailylimitexceeded", "dailylimitexceededunreg"})

FOLDER_MIME = "application/vnd.google-apps.folder"

#: Chunks in a row Drive may keep none of before an upload gives up for now.
STALL_LIMIT = 5


class DriveError(RuntimeError):
    """Drive said no. ``status`` is the HTTP code, when there was one.

    ``account_wide`` marks a failure that is about the account or the
    connection rather than the file being sent — a rate limit, a full Drive,
    Google out of reach. Every other file would fail the same way, so the
    uploader stops and waits rather than counting it against each one.
    """

    def __init__(self, message: str, status: int = 0, retryable: bool = False,
                 *, account_wide: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.account_wide = account_wide


class NeedsReconnect(DriveError):
    """The refresh token is gone or revoked — only a person can fix this."""


# ---------------------------------------------------------------------------
# The one place this module speaks HTTP
# ---------------------------------------------------------------------------

_OPENER: urllib.request.OpenerDirector | None = None


def _get_opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is None:
        # urllib opens a new connection per request and always sends
        # "Connection: close" whatever is asked for, so there is no keep-alive
        # to be had here. Larger chunks (CHUNK) are what cut the handshakes.
        _OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler())
        _OPENER.addheaders = [("User-Agent", "Ninaivu/1")]
    return _OPENER


def _request(method: str, url: str, *, headers: dict[str, str] | None = None,
             body: bytes | None = None,
             timeout: float = 60.0) -> tuple[int, dict[str, str], bytes]:
    """One HTTP round trip. Returns ``(status, headers, body)``.

    Errors are returned rather than raised, because Drive's protocol uses
    status codes as answers: a 308 means "keep going with this upload", a 404
    on a resume URL means "that upload expired, start again". Raising on those
    would turn ordinary flow control into exception handling.
    """
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _get_opener().open(request, timeout=timeout) as response:
            return (response.status, dict(response.headers),
                    response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        # No route to Google at all — no connection, no name lookup — is the
        # same for every file, and is not charged to the one in hand.
        raise DriveError(f"could not reach Google: {exc.reason}",
                         retryable=True, account_wide=True) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise DriveError("Google did not answer in time",
                         retryable=True) from exc
    except (ConnectionError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise DriveError(f"network error communicating with Google: {exc}",
                         retryable=True) from exc


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass
class Credentials:
    """What Ninaivu keeps about a connected Google account.

    The refresh token is the sensitive part: it is a long-lived key to the
    scope above. It lives in a file in the state directory with owner-only
    permissions, it is never sent to the browser, and it is never written to a
    log — :meth:`public` is what the console gets.
    """

    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    access_token: str = ""
    expires_at: float = 0.0
    account: str = ""
    folder_id: str = ""
    #: Empty until chosen, so a new connection takes ``cloud_folder_name`` (or
    #: NINAIVU_CLOUD_FOLDER); "Ninaivu" here meant that setting was never read.
    folder_name: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return bool(self.refresh_token)

    @property
    def fresh(self) -> bool:
        # A minute's margin: a token that expires while a large upload is in
        # flight is a failure that looks like a network fault.
        return bool(self.access_token) and time.time() < self.expires_at - 60

    def public(self) -> dict[str, Any]:
        """Everything the console may see. No tokens, ever."""
        return {
            "configured": self.configured,
            "connected": self.connected,
            "account": self.account,
            "folder_name": self.folder_name,
            "folder_id": self.folder_id,
            # Enough of the client ID to recognise which Google project this
            # is, and no more. The *front* of a client ID is the project
            # number, which is the part that identifies it — the tail is
            # ".apps.googleusercontent.com" on every client ever issued and
            # would tell an administrator nothing at all.
            "client_id_hint": (f"{self.client_id[:14]}…"
                               if len(self.client_id) > 14 else self.client_id),
        }

    # -- on disk ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Credentials":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Created owner-only, rather than written at the default permissions
        # and narrowed afterwards: between those two steps the refresh token
        # sat in a file anybody on the machine could read. mkstemp makes the
        # file 0600 and new (O_EXCL), and a name of its own per save means two
        # uploads saving at once cannot write into, or rename, each other's.
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                            dir=str(target.parent))
        tmp = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.__dict__, handle, indent=2)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # The fixed name earlier versions wrote through may have been left by
        # an interrupted save, at the default permissions, token and all.
        try:
            target.with_suffix(target.suffix + ".tmp").unlink()
        except OSError:
            pass


def consent_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Where to send the browser to ask the person for permission.

    ``access_type=offline`` with ``prompt=consent`` is what makes Google hand
    back a *refresh* token rather than an hour-long access token — without it
    the connection would quietly stop working after an hour and look like a
    bug in the uploader.
    """
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    return f"{AUTH_URL}?{query}"


def new_state() -> str:
    """An unguessable value tying a callback to the request that started it."""
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

@dataclass
class DriveClient:
    """Talks to Drive on behalf of one connected account."""

    creds: Credentials
    transport: Callable[..., tuple[int, dict[str, str], bytes]] = field(
        default=_request)
    on_change: Callable[[Credentials], None] | None = None
    #: Several uploads share this client. When the token runs out they would
    #: each refresh it, and each save the credentials file at the same moment.
    _refreshing: threading.Lock = field(default_factory=threading.Lock, repr=False,
                                        compare=False)

    # -- tokens -----------------------------------------------------------

    def exchange_code(self, code: str, redirect_uri: str) -> Credentials:
        """Turn the one-time code from the consent screen into tokens."""
        status, _, body = self.transport(
            "POST", TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode({
                "code": code,
                "client_id": self.creds.client_id,
                "client_secret": self.creds.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }).encode())
        data = _json(body)
        if status != 200 or "access_token" not in data:
            raise DriveError(_why(data, "Google refused the sign-in"), status)
        if not data.get("refresh_token"):
            # Without one, the connection dies in an hour and the household
            # finds out days later. Better to fail the sign-in now.
            raise DriveError(
                "Google did not return a refresh token. Remove Ninaivu from "
                "your account's third-party access and connect again.", status)

        self.creds.refresh_token = data["refresh_token"]
        self.creds.access_token = data["access_token"]
        self.creds.expires_at = time.time() + float(data.get("expires_in", 3600))
        self.creds.account = self._whoami()
        self._changed()
        return self.creds

    def refresh(self) -> None:
        """Get a new access token from the refresh token."""
        if not self.creds.refresh_token:
            raise NeedsReconnect("No Google account is connected.")
        status, _, body = self.transport(
            "POST", TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode({
                "refresh_token": self.creds.refresh_token,
                "client_id": self.creds.client_id,
                "client_secret": self.creds.client_secret,
                "grant_type": "refresh_token",
            }).encode())
        data = _json(body)
        if status == 400 or data.get("error") == "invalid_grant":
            # Revoked, expired, or the password changed. Nothing automatic can
            # recover this, so say so instead of retrying for ever.
            raise NeedsReconnect(
                "Google has stopped accepting the saved permission. Connect "
                "the account again.")
        if status != 200 or "access_token" not in data:
            raise DriveError(_why(data, "Could not refresh the Google token"),
                             status, retryable=status in RETRY_STATUS)
        self.creds.access_token = data["access_token"]
        self.creds.expires_at = time.time() + float(data.get("expires_in", 3600))
        self._changed()

    def _auth_header(self) -> dict[str, str]:
        if not self.creds.fresh:
            with self._refreshing:
                if not self.creds.fresh:
                    self.refresh()
        return {"Authorization": f"Bearer {self.creds.access_token}"}

    def _call(self, method: str, url: str, *, headers: dict[str, str] | None = None,
              body: bytes | None = None,
              timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
        """One signed request to Drive, renewing the access token once on a 401.

        The token's expiry is only what Google said when it was issued; one
        revoked early, or a clock that disagrees with Google's, is answered
        with 401. That used to come back as an ordinary refusal of the file
        in hand — counted against it, and against every file after it. Here
        it is renewed once and the request made again; a second 401 means the
        permission itself is no good, which only a person can fix.
        """
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        for attempt in range(2):
            auth = self._auth_header()
            status, got, data = self.transport(
                method, url, headers={**auth, **(headers or {})}, body=body, **extra)
            if status != 401:
                return status, got, data
            if attempt == 0:
                used = auth["Authorization"].split(" ", 1)[-1]
                with self._refreshing:
                    # Another upload sharing this client may have renewed it
                    # already; one renewal is enough for all of them.
                    if self.creds.access_token == used:
                        self.creds.expires_at = 0.0
                        self.refresh()
        raise NeedsReconnect(
            "Google turned down the saved permission even after renewing it. "
            "Connect the account again.", 401)

    def _whoami(self) -> str:
        try:
            status, _, body = self.transport(
                "GET", USERINFO_URL,
                headers={"Authorization": f"Bearer {self.creds.access_token}"})
            if status == 200:
                return str(_json(body).get("email", ""))
        except DriveError:
            pass
        return ""

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self.creds)

    # -- folders ----------------------------------------------------------
    #
    # Everything Ninaivu does in Drive happens inside one folder, and the code
    # below is arranged so that straying outside it is not a thing that can
    # happen by accident. `ensure_folder` will not run a search without a
    # parent, `folder_path` will not start without a root, and `begin_upload`
    # will not send a file with nowhere to put it. The only function allowed to
    # look at the top level of somebody's Drive is `ninaivu_root`, it looks for
    # exactly one name, and what it finds is checked before it is used.

    def ninaivu_root(self) -> str:
        """The id of the one folder Ninaivu owns. Everything goes under it.

        A saved id is verified before it is trusted: it has to still exist, be
        a folder, and not be in the bin. A stale or edited id therefore fails
        safe — the folder is found or made again — rather than turning into an
        upload into somebody's tax return.

        Renaming that folder in Drive is *not* treated as losing it. Somebody
        tidying up and calling it "Family photos" has not asked for a second
        folder and a divided backup; the id is what identifies it, and the new
        name is adopted here so the console shows what is actually in Drive.
        """
        saved = self.creds.folder_id
        if saved and self._is_our_root(saved):
            return saved

        name = self.creds.folder_name or "Ninaivu"
        found = self._find_folder(name, parent="root")
        folder_id = found or self._create_folder(name, parent="")
        if folder_id != self.creds.folder_id:
            self.creds.folder_id = folder_id
            self._changed()
        return folder_id

    def _is_our_root(self, folder_id: str) -> bool:
        """Is this id still the Ninaivu folder, and still where we left it?"""
        fields = "id,name,mimeType,trashed,parents"
        status, _, body = self._call(
            "GET", f"{API_URL}/{urllib.parse.quote(folder_id)}"
                   f"?fields={urllib.parse.quote(fields)}")
        if status == 403 and _failure(status, body, "").retryable:
            # Slowed down, not refused: the folder may be perfectly good.
            raise _failure(status, body, "Could not check the Drive folder")
        if status in (404, 403):
            return False
        if status != 200:
            # A rate limit or an outage is not evidence that the folder is
            # wrong, and re-creating it on a bad afternoon would leave the
            # household with two. Say so and let the caller back off.
            raise _failure(status, body, "Could not check the Drive folder")
        data = _json(body)
        if data.get("mimeType") != FOLDER_MIME or data.get("trashed"):
            return False
        name = str(data.get("name", ""))
        if name and name != self.creds.folder_name:
            # Renamed in Drive. Follow it rather than making a second folder.
            self.creds.folder_name = name
            self._changed()
        return True

    def _find_folder(self, name: str, parent: str) -> str:
        """Look for one folder by name inside one parent. Nowhere else."""
        if not parent:
            raise DriveError("refusing to search Drive without a parent folder")
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (f"mimeType='{FOLDER_MIME}' and name='{escaped}' and "
                 f"trashed=false and '{parent}' in parents")
        status, _, body = self._call(
            "GET", f"{API_URL}?{urllib.parse.urlencode({'q': query, 'fields': 'files(id,name)'})}")
        if status == 200:
            files = _json(body).get("files") or []
            return str(files[0]["id"]) if files else ""
        if status == 404:
            return ""
        raise _failure(status, body, "Could not look in Drive")

    def _create_folder(self, name: str, parent: str) -> str:
        metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            metadata["parents"] = [parent]
        status, _, body = self._call(
            "POST", f"{API_URL}?fields=id",
            headers={"Content-Type": "application/json"},
            body=json.dumps(metadata).encode())
        data = _json(body)
        if status not in (200, 201) or "id" not in data:
            raise _failure(status, data, "Could not make the Drive folder")
        return str(data["id"])

    def ensure_folder(self, name: str, parent: str) -> str:
        """The id of a folder with this name *inside this parent*.

        The parent is not optional. With the ``drive.file`` scope a search
        already only sees folders Ninaivu itself created, but "only ours" and
        "only inside ours" are different promises, and this is the one that was
        asked for: a folder called ``2019`` that Ninaivu made under some other
        application's tree must never be adopted as the place to put July.
        """
        if not parent:
            raise DriveError("refusing to make a folder outside the Ninaivu "
                             "folder")
        return self._find_folder(name, parent) or self._create_folder(name, parent)

    def folder_path(self, parts: list[str], root: str) -> str:
        """Make a chain of folders under ``root`` and return the last one."""
        if not root:
            raise DriveError("refusing to make folders outside the Ninaivu "
                             "folder")
        parent = root
        for part in parts:
            part = str(part).strip().strip("/")
            # A component that tried to climb out of the tree is not honoured
            # and not guessed at; it is simply not a folder.
            if part and part not in (".", ".."):
                parent = self.ensure_folder(part, parent)
        return parent

    # -- uploading --------------------------------------------------------

    def begin_upload(self, name: str, parent: str, size: int,
                     mime: str = "application/octet-stream") -> str:
        """Start a resumable upload. Returns the URL to send the bytes to."""
        if not parent:
            # Without a parent Drive puts the file in the top level of the
            # account, which is precisely the one place it must never go.
            raise DriveError("refusing to upload outside the Ninaivu folder")
        metadata: dict[str, Any] = {"name": name, "parents": [parent]}
        payload = json.dumps(metadata).encode()
        status, headers, body = self._call(
            "POST", f"{UPLOAD_URL}?uploadType=resumable&fields=id",
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(size),
            },
            body=payload)
        if status not in (200, 201):
            raise _failure(status, body, "Could not start the upload")
        location = headers.get("Location") or headers.get("location")
        if not location:
            raise DriveError("Google did not say where to send the file",
                             status, retryable=True)
        return location

    def send_chunk(self, url: str, chunk: bytes, offset: int,
                   total: int) -> tuple[bool, int, str]:
        """Send one chunk. Returns ``(finished, next offset, remote id)``.

        Drive answers a partial upload with 308 and a ``Range`` header saying
        how much it actually kept — which is not always what was sent, so the
        next offset comes from Google rather than from arithmetic here.
        """
        last = offset + len(chunk) - 1
        status, headers, body = self.transport(
            "PUT", url,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{last}/{total}",
            },
            body=chunk, timeout=300.0)

        if status in (200, 201):
            return True, total, str(_json(body).get("id", ""))
        if status == 308:
            rng = headers.get("Range") or headers.get("range") or ""
            if "-" in rng:
                try:
                    return False, int(rng.rsplit("-", 1)[1]) + 1, ""
                except ValueError:
                    pass
            # No Range header: Drive kept none of this chunk. Assuming it had
            # kept all of it moved the offset past bytes Drive never received,
            # and the file that landed was missing them. Same answer as
            # resume_status gives: no progress, send it again.
            return False, offset, ""
        if status in (404, 410):
            # The resumable session expired. Not retryable at this URL, but the
            # file itself is fine — the caller starts a new session.
            raise DriveError("that upload expired; starting it again",
                             status, retryable=True)
        raise _failure(status, body, "Upload refused")

    def upload_file(self, path: str | Path, name: str, parent: str, *,
                    resume_url: str = "",
                    on_progress: Callable[[int, int], None] | None = None,
                    on_session: Callable[[str], None] | None = None,
                    digest: Any = None,
                    chunk_size: int | Callable[[], int] = 0,
                    gate: Callable[[int], None] | None = None) -> str:
        """Put one file up, in chunks, resuming if we can. Returns its id.

        ``digest`` may be a hashlib object. It is fed the bytes Drive actually
        *kept* — not the bytes offered — so a short write, where the next
        chunk is re-read from an earlier offset, does not hash the overlap
        twice. That is how the record gets a hash of what went up without a
        second full read of the file: on a library of large videos, hashing
        afterwards would double the disk work for a field nobody is waiting on.

        It is only a hash of the whole file if the upload started at the
        beginning, which is the caller's business to know — it is the one
        holding the resume URL.

        ``gate`` is called with the size of each chunk just before it goes, and
        is how a caller imposes a speed limit or a stop without this module
        knowing what either of those is. It may block, and it may raise — an
        exception from it comes straight back out of here, leaving a resumable
        session behind rather than a half-written file.

        ``chunk_size`` overrides :data:`CHUNK` for this upload. A caller
        enforcing a low ceiling wants smaller chunks, so the limit is
        approached smoothly instead of in bursts at line speed.
        A callable is evaluated before each read to adapt during large files.
        """
        size = os.path.getsize(path)
        url = resume_url or self.begin_upload(name, parent, size, _mime(name))
        if on_session and url != resume_url:
            on_session(url)

        offset = 0
        stalled = 0
        if resume_url:
            try:
                for attempt in range(4):
                    try:
                        offset, completed_id = self.resume_status(url, size)
                        break
                    except DriveError as exc:
                        if (not exc.retryable or attempt == 3
                                or (exc.status not in ({0} | RETRY_STATUS)
                                    and not _is_rate_limit(exc))):
                            raise
                        time.sleep(0.25 * (2 ** attempt))
                if completed_id:
                    if on_progress:
                        on_progress(size, size)
                    return completed_id
            except DriveError as exc:
                if exc.status in (404, 410):
                    url = self.begin_upload(name, parent, size, _mime(name))
                    if on_session:
                        on_session(url)
                    offset = 0
                else:
                    raise

        with open(path, "rb") as handle:
            while True:
                limit = chunk_size() if callable(chunk_size) else chunk_size
                limit = limit if limit and limit > 0 else CHUNK
                handle.seek(offset)
                chunk = handle.read(limit)
                if not chunk:
                    # Nothing left to send but Drive never said "finished".
                    raise DriveError("the upload ended without confirmation",
                                     retryable=True)
                if gate is not None:
                    gate(len(chunk))
                before = offset

                # Intra-chunk transient retry for network stability
                max_retries = 3
                attempt = 0
                while True:
                    try:
                        done, offset, remote_id = self.send_chunk(url, chunk, offset, size)
                        break
                    except DriveError as exc:
                        if (exc.retryable and (exc.status in (0, 429, 500, 502, 503, 504)
                                               or _is_rate_limit(exc))
                                and attempt < max_retries):
                            attempt += 1
                            time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
                            continue
                        raise

                if digest is not None:
                    digest.update(chunk[:max(0, min(offset - before, len(chunk)))])
                if on_progress:
                    on_progress(min(offset, size), size)
                if done:
                    return remote_id
                # A chunk Drive kept none of is sent again, but not for ever:
                # a session that keeps answering "nothing kept" is broken, and
                # looping on it would hold the upload thread indefinitely.
                stalled = stalled + 1 if offset <= before else 0
                if stalled >= STALL_LIMIT:
                    raise DriveError("Google kept none of the last few pieces sent; "
                                     "trying again later", retryable=True)

    def resume_offset(self, url: str, size: int) -> int:
        """Ask Drive how much of an interrupted upload it already has."""
        return self.resume_status(url, size)[0]

    def resume_status(self, url: str, size: int) -> tuple[int, str]:
        """Return acknowledged bytes and, for a completed upload, its ID."""
        status, headers, body = self.transport(
            "PUT", url,
            headers={"Content-Length": "0",
                     "Content-Range": f"bytes */{size}"})
        if status in (200, 201):
            remote_id = str(_json(body).get("id", ""))
            if not remote_id:
                raise DriveError("completed upload did not return a file ID", retryable=True)
            return size, remote_id
        if status in (404, 410):
            raise DriveError("that upload expired; starting it again",
                             status, retryable=True)
        rng = headers.get("Range") or headers.get("range") or ""
        if status == 308 and "-" in rng:
            try:
                return int(rng.rsplit("-", 1)[1]) + 1, ""
            except ValueError:
                return 0, ""
        if status == 308:
            return 0, ""
        raise _failure(status, body, "Upload status refused")

    def begin_update(self, file_id: str, size: int,
                     mime: str = "application/octet-stream", *,
                     name: str | None = None) -> str:
        """Start a resumable upload of new contents for a file Ninaivu made.

        For the copy of the index kept in Drive, which is replaced rather than
        added to: Ninaivu deletes nothing in Drive, so a copy that was added
        every day would pile up for ever. Drive keeps the earlier contents as
        revisions for a while, which is a second way back if an update is bad.
        """
        if not file_id:
            raise DriveError("refusing to update a file without its id")
        status, headers, body = self._call(
            "PATCH", f"{UPLOAD_URL}/{urllib.parse.quote(file_id)}"
                     f"?uploadType=resumable&fields=id",
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(size),
            },
            # A new name with the new contents: a copy that becomes encrypted
            # says so, rather than keeping the name of the plain one it replaced.
            body=json.dumps({"name": name} if name else {}).encode())
        location = headers.get("Location") or headers.get("location") or ""
        if status == 200 and location:
            return location
        if status == 404:
            raise DriveError("the file to update is no longer in Google Drive", 404)
        raise _failure(status, body, "Google refused the update")

    # -- reading back ------------------------------------------------------
    #
    # The ``drive.file`` scope that limits uploads to Ninaivu's own folder also
    # lets Ninaivu read back exactly what it put there, and nothing else. That
    # is all a restore needs.

    def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        """Everything directly inside one folder: files and folders, not in the bin.

        Each entry has ``id``, ``name``, ``mimeType`` and, for a file, ``size``
        and ``md5Checksum`` as Drive reports them.
        """
        if not folder_id:
            raise DriveError("refusing to list Drive without a folder")
        escaped = folder_id.replace("\\", "\\\\").replace("'", "\\'")
        found: list[dict[str, Any]] = []
        token = ""
        while True:
            params = {
                "q": f"'{escaped}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)",
                "pageSize": "1000",
            }
            if token:
                params["pageToken"] = token
            status, _, body = self._call(
                "GET", f"{API_URL}?{urllib.parse.urlencode(params)}")
            if status != 200:
                raise _failure(status, body, "Could not list the Drive folder")
            data = _json(body)
            found += [f for f in data.get("files") or [] if isinstance(f, dict)]
            token = str(data.get("nextPageToken") or "")
            if not token:
                return found

    def file_info(self, file_id: str) -> dict[str, Any]:
        """Name, size, checksum and whether it is in the bin, for one file."""
        fields = "id,name,mimeType,size,md5Checksum,trashed"
        status, _, body = self._call(
            "GET", f"{API_URL}/{urllib.parse.quote(file_id)}"
                   f"?fields={urllib.parse.quote(fields)}")
        if status == 404:
            raise DriveError("the file is no longer in Google Drive", 404)
        if status != 200:
            raise _failure(status, body, "Could not look up the file")
        return _json(body)

    def download_range(self, file_id: str, start: int, end: int) -> bytes:
        """Bytes ``start``..``end`` inclusive of one file Ninaivu uploaded.

        In ranges rather than one response, because a response is read whole
        and a family video can be several gigabytes; a range also makes an
        interrupted download resumable from the bytes already on disk.
        """
        status, _, body = self._call(
            "GET", f"{API_URL}/{urllib.parse.quote(file_id)}?alt=media",
            headers={"Range": f"bytes={start}-{end}"},
            timeout=300.0)
        if status == 206:
            return body
        if status == 200:
            # The whole file, because the range was ignored.
            return body[start:end + 1]
        if status == 416:
            return b""
        if status == 404:
            raise DriveError("the file is no longer in Google Drive", 404)
        raise _failure(status, body, "Google refused the download")

    def revoke(self) -> None:
        """Hand the permission back to Google. Best effort."""
        token = self.creds.refresh_token or self.creds.access_token
        if not token:
            return
        try:
            self.transport(
                "POST", "https://oauth2.googleapis.com/revoke",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=urllib.parse.urlencode({"token": token}).encode())
        except DriveError:
            # Disconnecting locally has to work even when Google cannot be
            # reached; the credentials are being deleted here either way.
            log.debug("could not revoke the token with Google")


# ---------------------------------------------------------------------------

def _json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _reasons(data: dict[str, Any]) -> set[str]:
    """Google's machine-readable reasons for an error, normalised.

    Drive v3 puts them in ``error.errors[].reason`` (``userRateLimitExceeded``);
    newer Google APIs in ``error.details[].reason`` (``RATE_LIMIT_EXCEEDED``).
    Both are read, lower-cased and without underscores.
    """
    error = data.get("error")
    if not isinstance(error, dict):
        return set()
    found = set()
    for key in ("errors", "details"):
        for entry in error.get(key) or []:
            if isinstance(entry, dict) and entry.get("reason"):
                found.add(str(entry["reason"]).replace("_", "").lower())
    return found


def _failure(status: int, body: bytes | dict[str, Any], fallback: str) -> DriveError:
    """The DriveError for an unsuccessful answer, retryable where Google says so.

    A 403 is Drive's answer both to "you may not" and to "not so fast"; the
    reason in the body is what tells them apart. The second is retried with
    backoff like a 429, and both it and a full Drive are marked
    ``account_wide`` so they are not charged against the file in hand.
    """
    data = body if isinstance(body, dict) else _json(body)
    reasons = _reasons(data)
    limited = status == 429 or (status == 403 and bool(reasons & RATE_LIMIT_REASONS))
    account = limited or (status == 403 and bool(reasons & ACCOUNT_REASONS))
    return DriveError(_why(data, fallback), status,
                      retryable=status in RETRY_STATUS or limited,
                      account_wide=account)


def _is_rate_limit(exc: DriveError) -> bool:
    """A 403 that is Google asking to slow down (see :func:`_failure`)."""
    return exc.status == 403 and exc.retryable


def _why(data: dict[str, Any], fallback: str) -> str:
    """Google's own words for what went wrong, when it gave any."""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or fallback)
    if isinstance(error, str):
        detail = data.get("error_description")
        return f"{error}: {detail}" if detail else error
    return fallback


_MIME = {
    # Images
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif", ".jfif": "image/jpeg",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    # Camera RAW
    ".cr2": "image/x-canon-cr2", ".cr3": "image/x-canon-cr3",
    ".nef": "image/x-nikon-nef", ".arw": "image/x-sony-arw",
    ".dng": "image/x-adobe-dng", ".orf": "image/x-olympus-orf",
    ".rw2": "image/x-panasonic-rw2", ".raf": "image/x-fuji-raf",
    ".srw": "image/x-samsung-srw",
    # Videos
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".3gp": "video/3gpp",
    ".ts": "video/mp2t", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    # Audio
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".wav": "audio/wav", ".flac": "audio/flac", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/opus",
    ".wma": "audio/x-ms-wma", ".aiff": "audio/x-aiff", ".alac": "audio/alac",
}


def _mime(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in _MIME:
        return _MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"
