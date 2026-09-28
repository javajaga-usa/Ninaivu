"""A stand-in for Google Drive, faithful enough to be worth testing against.

Google is not reachable from a test run, and it should not be: a suite that
needs somebody's real Drive account is a suite nobody can run. What can be done
instead is to implement the parts of the protocol Ninaivu depends on, including
the awkward parts, and drive the real client code through them.

What it reproduces, because each one is a thing the client has to get right:

* the token endpoint, including ``invalid_grant`` for a revoked permission and
  the missing-refresh-token case that would otherwise die silently in an hour
* resumable uploads: a session URL, chunked ``Content-Range`` puts, ``308``
  with a ``Range`` header that may report *less* than was sent, and a final
  ``200`` carrying the file id
* sessions that expire, answering ``404`` so the client has to start again
* ``429`` and ``503``, so backoff is exercised rather than assumed
* folder search and creation under the ``drive.file`` scope

What it does not reproduce is Google's judgement: quota rules, abuse limits,
and whatever a real account does on a bad day. So this proves the client
implements the protocol it was written against — not that a real upload works.
That distinction is worth keeping in mind, and is why the README says the cloud
sync has never been run against Google itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from typing import Any


class FakeDrive:
    """Records everything, so a test can ask what actually happened."""

    def __init__(self):
        self.files: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.uploads: list[dict[str, Any]] = []
        self.created: list[str] = []          # folder ids made through the API
        self.revoked: list[str] = []
        self.next_id = 1

        # Things a test can arrange.
        self.refresh_token = "refresh-token-1"
        self.give_refresh_token = True
        self.invalid_grant = False
        self.fail_next: list[int] = []       # status codes to return, in order
        self.short_write = 0                 # bytes to "lose" on the next chunk
        self.expire_next_session = False
        self.access_token_life = 3600.0
        self.calls: list[tuple[str, str]] = []
        self.downloads: list[str] = []
        self.updates: list[str] = []             # files given new contents
        self.clock = 0
        self.corrupt_download = False        # flip a byte of what is served
        self.last_headers: dict[str, str] = {}

    # -- the transport the client is given --------------------------------

    def __call__(self, method: str, url: str, *, headers=None, body=None,
                 timeout: float = 60.0):
        headers = headers or {}
        self.calls.append((method, url.split("?")[0]))
        self.last_headers = headers

        if self.fail_next:
            status = self.fail_next.pop(0)
            return status, {}, json.dumps(
                {"error": {"message": "slow down"}}).encode()

        if url.startswith("https://oauth2.googleapis.com/token"):
            return self._token(body)
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            self.revoked.append("token")
            return 200, {}, b"{}"
        if url.startswith("https://www.googleapis.com/oauth2/v2/userinfo"):
            return 200, {}, json.dumps({"email": "dad@example.com"}).encode()
        if url.startswith("https://www.googleapis.com/upload/drive/v3/files"):
            return self._begin(headers, body, method=method, url=url)
        if url.startswith("https://upload.example/session/"):
            return self._chunk(url, headers, body)
        if url.startswith("https://www.googleapis.com/drive/v3/files"):
            return self._files(method, url, body)
        return 404, {}, b"{}"

    # -- pieces -----------------------------------------------------------

    def _token(self, body):
        form = urllib.parse.parse_qs((body or b"").decode())
        if self.invalid_grant:
            return 400, {}, json.dumps({"error": "invalid_grant"}).encode()
        payload: dict[str, Any] = {
            "access_token": f"access-{time.time():.0f}",
            "expires_in": self.access_token_life,
            "token_type": "Bearer",
        }
        if form.get("grant_type", [""])[0] == "authorization_code":
            if self.give_refresh_token:
                payload["refresh_token"] = self.refresh_token
        return 200, {}, json.dumps(payload).encode()

    def _files(self, method, url, body):
        path = urllib.parse.urlparse(url).path
        tail = path.split("/drive/v3/files", 1)[1].lstrip("/")

        if method == "GET" and tail and urllib.parse.unquote(tail) in self.files:
            # A file Ninaivu uploaded: its bytes (a range of them), or about it.
            fid = urllib.parse.unquote(tail)
            data = self.files[fid]["bytes"]
            if "alt=media" in url:
                self.downloads.append(fid)
                wanted = re.match(r"bytes=(\d+)-(\d+)",
                                  self.last_headers.get("Range", ""))
                if not wanted:
                    return 200, {}, data
                start, end = int(wanted.group(1)), int(wanted.group(2))
                if start >= len(data):
                    return 416, {}, b""
                piece = data[start:end + 1]
                if self.corrupt_download and start == 0:
                    piece = bytes([piece[0] ^ 0xFF]) + piece[1:]
                return 206, {}, piece
            return 200, {}, json.dumps(self._describe(fid)).encode()

        if method == "GET" and tail:
            # Metadata for one id — how the client checks a remembered folder
            # is still the folder it thinks it is.
            folder = self.folders.get(urllib.parse.unquote(tail))
            if folder is None:
                return 404, {}, json.dumps(
                    {"error": {"message": "File not found"}}).encode()
            return 200, {}, json.dumps({
                "id": tail,
                "name": folder["name"],
                "mimeType": "application/vnd.google-apps.folder",
                "trashed": bool(folder.get("trashed")),
                "parents": folder.get("parents") or [],
            }).encode()

        if method == "GET":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            wanted = query.get("q", [""])[0]
            found = re.search(r"name='((?:\\.|[^'\\])*)'", wanted)
            name = (found.group(1).replace("\\'", "'").replace("\\\\", "\\")
                    if found else "")
            in_parents = re.search(r"'([^']*)' in parents", wanted)
            parent = in_parents.group(1) if in_parents else ""
            if not found:
                # Everything in one folder: how a restore walks the tree.
                inside = [{"id": fid, "name": f["name"],
                           "mimeType": "application/vnd.google-apps.folder"}
                          for fid, f in self.folders.items()
                          if parent in (f.get("parents") or [])
                          and not f.get("trashed")]
                inside += [self._describe(fid) for fid, f in self.files.items()
                           if parent in (f.get("parents") or [])]
                return 200, {}, json.dumps({"files": inside}).encode()
            hits = [{"id": fid, "name": f["name"]}
                    for fid, f in self.folders.items()
                    if f["name"] == name and not f.get("trashed")
                    and self._under(f, parent)]
            return 200, {}, json.dumps({"files": hits}).encode()

        data = json.loads((body or b"{}").decode())
        fid = f"folder-{self.next_id}"
        self.next_id += 1
        self.folders[fid] = {"name": data.get("name", ""),
                             "parents": data.get("parents") or ["root"]}
        self.created.append(fid)
        return 200, {}, json.dumps({"id": fid}).encode()

    def _describe(self, fid: str) -> dict[str, Any]:
        data = self.files[fid]["bytes"]
        return {"id": fid, "name": self.files[fid]["name"],
                "mimeType": "application/octet-stream", "size": str(len(data)),
                "md5Checksum": hashlib.md5(data).hexdigest(), "trashed": False,
                "modifiedTime": self.files[fid].get("modified", "")}

    @staticmethod
    def _under(folder, parent):
        """Does this folder sit in ``parent``? ``root`` means the top level."""
        parents = folder.get("parents") or []
        if parent in ("", "root"):
            return not parents or "root" in parents
        return parent in parents

    def _begin(self, headers, body, method="POST", url=""):
        meta = json.loads((body or b"{}").decode())
        sid = f"session-{self.next_id}"
        self.next_id += 1
        # PATCH .../files/<id>: new contents for a file that already exists.
        target = urllib.parse.urlparse(url).path.split("/drive/v3/files", 1)[1].strip("/")
        updating = urllib.parse.unquote(target) if method == "PATCH" and target else ""
        if updating and updating not in self.files:
            return 404, {}, json.dumps({"error": {"message": "File not found"}}).encode()
        self.sessions[sid] = {
            "update": updating,
            "name": meta.get("name", "") or (self.files[updating]["name"] if updating else ""),
            "parents": meta.get("parents", []),
            "total": int(headers.get("X-Upload-Content-Length", 0)),
            "received": 0,
            "data": b"",
            "dead": self.expire_next_session,
        }
        self.expire_next_session = False
        return 200, {"Location": f"https://upload.example/session/{sid}"}, b"{}"

    def _chunk(self, url, headers, body):
        sid = url.rsplit("/", 1)[1]
        session = self.sessions.get(sid)
        if session is None or session["dead"]:
            return 404, {}, json.dumps(
                {"error": {"message": "session expired"}}).encode()

        rng = headers.get("Content-Range", "")
        # "bytes */total" is the client asking how much we have.
        if rng.startswith("bytes */"):
            if session["received"] >= session["total"]:
                return 200, {}, json.dumps({"id": self._land(sid)}).encode()
            return 308, {"Range": f"bytes=0-{session['received'] - 1}"}, b""

        payload = body or b""
        # Losing a few bytes is legal and the client has to notice: Drive
        # reports what it kept, not what was sent.
        keep = len(payload) - min(self.short_write, len(payload))
        self.short_write = 0
        session["data"] += payload[:keep]
        session["received"] = len(session["data"])

        if session["received"] >= session["total"]:
            return 200, {}, json.dumps({"id": self._land(sid)}).encode()
        return 308, {"Range": f"bytes=0-{session['received'] - 1}"}, b""

    def _land(self, sid: str) -> str:
        session = self.sessions[sid]
        self.clock += 1
        stamp = f"2026-09-25T00:00:{self.clock:02d}.000Z"
        if session.get("update"):
            fid = session["update"]
            self.files[fid].update(name=session["name"], bytes=session["data"],
                                   modified=stamp)
            self.updates.append(fid)
            return fid
        fid = f"file-{self.next_id}"
        self.next_id += 1
        self.files[fid] = {"name": session["name"],
                           "parents": session["parents"],
                           "bytes": session["data"], "modified": stamp}
        self.uploads.append({"id": fid, "name": session["name"],
                             "size": len(session["data"])})
        return fid

    # -- what a test asks it ---------------------------------------------

    def uploaded_names(self) -> list[str]:
        return [u["name"] for u in self.uploads]

    def content(self, name: str) -> bytes:
        for f in self.files.values():
            if f["name"] == name:
                return f["bytes"]
        raise KeyError(name)

    def top_level_folder(self, name: str = "Ninaivu") -> str:
        for fid, folder in self.folders.items():
            if folder["name"] == name and self._under(folder, "root"):
                return fid
        return ""

    def _ancestors(self, parents: list[str]) -> set[str]:
        seen: set[str] = set()
        queue = list(parents)
        while queue:
            fid = queue.pop()
            if fid in seen:
                continue
            seen.add(fid)
            queue.extend(self.folders.get(fid, {}).get("parents") or [])
        return seen

    def outside(self, root_id: str) -> list[str]:
        """Names of everything created that is not inside ``root_id``.

        The question the containment tests actually want answered: did any of
        this land somewhere other than under the Ninaivu folder?
        """
        stray = []
        for fid in self.created:
            if fid == root_id or fid not in self.folders:
                continue
            if root_id not in self._ancestors(self.folders[fid]["parents"]):
                stray.append(self.folders[fid]["name"])
        for f in self.files.values():
            if root_id not in self._ancestors(f.get("parents") or []):
                stray.append(f["name"])
        return stray
