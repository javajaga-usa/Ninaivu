"""A small, careful client for the ComfyUI HTTP API.

Only the standard library, and only the handful of endpoints a job needs:
``/system_stats`` and ``/object_info`` to describe the server, ``/upload/image``
to hand it a photograph, ``/prompt`` to queue a workflow, ``/history`` to wait
for it, and ``/view`` to fetch what it made.

Photographs leave the Ninaivu machine through this module, so it is deliberately
narrow about where they can go:

* the address is validated when it is saved — http or https, a host, no
  credentials, no query string;
* **redirects are never followed**, so a server (or anything answering in its
  place) cannot bounce an upload to another address;
* every response is read with a size cap, and a job has a deadline after which
  it is removed from the server's queue.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import time
import uuid
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

from . import websocket

#: Largest JSON document accepted from the server. /object_info on a ComfyUI
#: with many custom nodes installed runs to a few megabytes.
MAX_JSON_BYTES = 32 * 1024 * 1024
#: Largest image accepted back from a job.
MAX_IMAGE_BYTES = 64 * 1024 * 1024


class AIServerError(RuntimeError):
    """Something about the AI server, phrased for the person who has to fix it."""


def normalise_url(value: Any) -> str:
    """The server address in one canonical form, or ValueError saying why not."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter the AI server's address, for example http://192.168.1.50:8188.")
    text = value.strip()
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("The AI server address must start with http:// or https://.")
    if not parts.hostname:
        raise ValueError("The AI server address needs a host name or IP address.")
    if parts.username or parts.password:
        raise ValueError("Leave the user name and password out of the AI server address.")
    if parts.query or parts.fragment:
        raise ValueError("The AI server address cannot have a query string or #fragment.")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("The AI server address has an invalid port.") from error
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{port}" if port else host
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


def network_scope(url: str) -> str:
    """``home``, ``public`` or ``unknown``: where this address points.

    Only literal IP addresses and the conventional home-network names are
    judged; any other host name is ``unknown`` rather than looked up, because a
    lookup made while saving says nothing about where it resolves later.
    """
    host = (urlsplit(url).hostname or "").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or "." not in host or host.endswith((".local", ".lan", ".home.arpa", ".internal")):
            return "home"
        return "unknown"
    if address.is_loopback or address.is_private or address.is_link_local:
        return "home"
    return "public"


class Client:
    """One ComfyUI server. Cheap to make; holds no connection between calls."""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        self.base = normalise_url(url)
        parts = urlsplit(self.base)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port
        self._prefix = parts.path
        self.timeout = timeout

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict[str, str] | None = None, limit: int = MAX_JSON_BYTES,
                 timeout: float | None = None) -> tuple[int, bytes, str]:
        kind = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = kind(self._host, self._port, timeout=timeout or self.timeout)
        try:
            conn.request(method, self._prefix + path, body=body, headers=headers or {})
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise AIServerError(
                    "The AI server answered with a redirect. Ninaivu does not follow "
                    "redirects with photographs; use the server's direct address.")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise AIServerError("The AI server sent more data than Ninaivu accepts.")
            return response.status, data, response.getheader("Content-Type") or ""
        except (OSError, http.client.HTTPException) as error:
            raise AIServerError(
                f"Cannot reach the AI server at {self.base}. Check that ComfyUI is running "
                f"there with --listen, and that its firewall allows this machine. ({error})"
            ) from error
        finally:
            conn.close()

    def _json(self, method: str, path: str, payload: Any = None,
              timeout: float | None = None) -> tuple[int, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        status, data, _ = self._request(method, path, body, headers, timeout=timeout)
        try:
            return status, json.loads(data) if data else None
        except ValueError as error:
            raise AIServerError(
                f"The server at {self.base} did not answer like ComfyUI (status {status}).") from error

    # -- describing the server ---------------------------------------------
    def system_stats(self) -> dict[str, Any]:
        status, data = self._json("GET", "/system_stats")
        if status != 200 or not isinstance(data, dict):
            raise AIServerError(f"The server at {self.base} did not answer like ComfyUI (status {status}).")
        system = data.get("system") if isinstance(data.get("system"), dict) else {}
        devices = []
        for device in data.get("devices") or []:
            if isinstance(device, dict):
                devices.append({
                    "name": str(device.get("name", ""))[:200],
                    "type": str(device.get("type", ""))[:40],
                    "vram_total": int(device.get("vram_total") or 0),
                    "vram_free": int(device.get("vram_free") or 0),
                })
        return {
            "comfyui_version": str(system.get("comfyui_version", ""))[:40],
            "pytorch_version": str(system.get("pytorch_version", ""))[:60],
            "devices": devices,
        }

    def node_types(self) -> set[str]:
        status, data = self._json("GET", "/object_info", timeout=max(self.timeout, 60))
        if status != 200 or not isinstance(data, dict):
            raise AIServerError("The AI server did not list its node types.")
        return {str(name) for name in data}

    # -- running a job -----------------------------------------------------
    def upload(self, data: bytes, suffix: str = "png") -> str:
        """Put an image in the server's input folder; returns the name to use."""
        filename = f"ninaivu-{uuid.uuid4().hex}.{suffix}"
        boundary = f"----ninaivu{uuid.uuid4().hex}"
        parts = []
        for name, value in (("type", "input"), ("overwrite", "false")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: image/{suffix}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        status, body, _ = self._request(
            "POST", "/upload/image", b"".join(parts),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            answer = json.loads(body)
        except ValueError:
            answer = None
        if status != 200 or not isinstance(answer, dict) or not answer.get("name"):
            raise AIServerError(f"The AI server refused the photo upload (status {status}).")
        subfolder = str(answer.get("subfolder") or "")
        return f"{subfolder}/{answer['name']}" if subfolder else str(answer["name"])

    def run(self, workflow: dict[str, Any], deadline: float,
            on_progress: Callable[[dict[str, Any]], None] | None = None) -> bytes:
        """Queue *workflow*, wait up to *deadline* seconds, return its first image.

        *on_progress*, if given, is called with ``{"stage": "queued", "ahead": n}``
        while the job waits and ``{"stage": "running", "value": v, "max": m}`` as
        it runs. Progress comes from ComfyUI's WebSocket when it can be opened,
        and from polling the queue when it cannot.
        """
        started = time.monotonic()
        client_id = uuid.uuid4().hex
        listener = self._listen(client_id) if on_progress else None
        try:
            return self._run(workflow, deadline, started, client_id, listener, on_progress)
        finally:
            if listener is not None:
                listener.close()

    def _listen(self, client_id: str):
        if self._https:
            return None
        try:
            return websocket.Listener(self._host, self._port,
                                      f"{self._prefix}/ws?clientId={client_id}", timeout=5)
        except OSError:
            return None                 # progress is optional; polling still works

    def _run(self, workflow, deadline, started, client_id, listener, on_progress) -> bytes:
        status, answer = self._json("POST", "/prompt",
                                    {"prompt": workflow, "client_id": client_id})
        if status != 200 or not isinstance(answer, dict) or not answer.get("prompt_id"):
            raise AIServerError(_prompt_error(answer, status))
        prompt_id = str(answer["prompt_id"])
        running = False
        last_queue_check = 0.0

        wait = 0.5
        while True:
            if on_progress and not running and time.monotonic() - last_queue_check > 2:
                last_queue_check = time.monotonic()
                running = self._report_queue(prompt_id, on_progress)
            _, history = self._json("GET", f"/history/{quote(prompt_id)}")
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                state = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                if state.get("status_str") == "error":
                    raise AIServerError(_execution_error(state))
                if state.get("completed", True) and entry.get("outputs"):
                    return self._first_image(entry["outputs"])
                if state.get("completed"):
                    raise AIServerError("The workflow finished without producing an image. "
                                        "Make sure it ends in a Save Image or Preview Image node.")
            if time.monotonic() - started > deadline:
                self._cancel(prompt_id)
                raise AIServerError(
                    f"The AI server did not finish within {int(deadline)} seconds, so the job "
                    "was cancelled. The server may be busy, or the timeout may need raising.")
            if listener is not None:
                running = self._drain(listener, prompt_id, wait, on_progress) or running
            else:
                time.sleep(wait)
            wait = min(2.0, wait * 1.5)

    def _report_queue(self, prompt_id: str, on_progress) -> bool:
        """Say how many jobs are ahead; True once this one is running."""
        try:
            _, queue = self._json("GET", "/queue")
        except AIServerError:
            return False
        if not isinstance(queue, dict):
            return False
        def ids(items):
            return [item[1] for item in items or [] if isinstance(item, list) and len(item) > 1]
        if prompt_id in ids(queue.get("queue_running")):
            on_progress({"stage": "running", "value": 0, "max": 0})
            return True
        pending = [item for item in queue.get("queue_pending") or []
                   if isinstance(item, list) and len(item) > 1]
        mine = next((item[0] for item in pending if item[1] == prompt_id), None)
        if mine is not None:
            ahead = len(ids(queue.get("queue_running"))) + sum(
                1 for item in pending if isinstance(item[0], (int, float)) and item[0] < mine)
            on_progress({"stage": "queued", "ahead": ahead})
        return False

    @staticmethod
    def _drain(listener, prompt_id: str, wait: float, on_progress) -> bool:
        """Read progress messages for up to *wait* seconds; True if this job ran."""
        running = False
        until = time.monotonic() + wait
        while True:
            remaining = until - time.monotonic()
            if remaining <= 0:
                return running
            try:
                text = listener.receive(remaining)
            except OSError:
                time.sleep(max(0.0, until - time.monotonic()))
                return running
            if text is None:
                return running
            try:
                message = json.loads(text)
            except ValueError:
                continue
            data = message.get("data") if isinstance(message, dict) else None
            if not isinstance(data, dict):
                continue
            kind = message.get("type")
            # Older ComfyUI sends progress without a prompt_id; it can only be
            # this job's once this job is the one running.
            owner = data.get("prompt_id")
            if owner != prompt_id and not (owner is None and kind == "progress" and running):
                continue
            if kind == "progress":
                running = True
                on_progress({"stage": "running", "value": int(data.get("value") or 0),
                             "max": int(data.get("max") or 0)})
            elif kind in ("execution_start", "executing") and data.get("node") is not None:
                running = True
                on_progress({"stage": "running", "value": 0, "max": 0})

    def _first_image(self, outputs: Any) -> bytes:
        found = []
        for node in (outputs.values() if isinstance(outputs, dict) else []):
            for image in (node.get("images") or []) if isinstance(node, dict) else []:
                if isinstance(image, dict) and image.get("filename"):
                    found.append(image)
        if not found:
            raise AIServerError("The workflow finished without producing an image. "
                                "Make sure it ends in a Save Image or Preview Image node.")
        # A saved result over a preview, when a workflow has both.
        found.sort(key=lambda image: image.get("type") != "output")
        chosen = found[0]
        query = urlencode({"filename": chosen["filename"], "subfolder": chosen.get("subfolder", ""),
                           "type": chosen.get("type", "output")})
        status, data, kind = self._request("GET", f"/view?{query}", limit=MAX_IMAGE_BYTES,
                                           timeout=max(self.timeout, 60))
        if status != 200 or not data:
            raise AIServerError(f"The AI server could not return the finished image (status {status}).")
        return data

    def _cancel(self, prompt_id: str) -> None:
        """Take the job out of the queue, and stop it if it is the one running."""
        try:
            self._json("POST", "/queue", {"delete": [prompt_id]})
            _, queue = self._json("GET", "/queue")
            running = queue.get("queue_running", []) if isinstance(queue, dict) else []
            if any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in running):
                self._json("POST", "/interrupt", {})
        except AIServerError:
            pass                # the deadline error is the one worth reporting


def _prompt_error(answer: Any, status: int) -> str:
    """Why ComfyUI refused to queue a workflow, as briefly as it can be said."""
    if isinstance(answer, dict):
        error = answer.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        details = []
        for node_id, node in (answer.get("node_errors") or {}).items():
            if not isinstance(node, dict):
                continue
            for problem in node.get("errors") or []:
                if isinstance(problem, dict):
                    details.append(f"node {node_id} ({node.get('class_type', '?')}): "
                                   f"{problem.get('message', '')} {problem.get('details', '')}".strip())
        if message or details:
            text = f"The AI server rejected the workflow: {message or 'invalid workflow'}"
            if details:
                text += " — " + "; ".join(details[:3])
            return text[:800]
    return f"The AI server rejected the workflow (status {status})."


def _execution_error(state: dict[str, Any]) -> str:
    for message in state.get("messages") or []:
        if isinstance(message, list) and len(message) == 2 and message[0] == "execution_error":
            data = message[1] if isinstance(message[1], dict) else {}
            return (f"The workflow failed on the AI server in {data.get('node_type', 'a node')}: "
                    f"{data.get('exception_message', 'unknown error')}".strip())[:800]
    return "The workflow failed on the AI server."
