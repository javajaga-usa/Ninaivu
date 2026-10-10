"""Asking a running Ninaivu to stop, the way every stop button does.

The shutdown endpoint takes the token the server wrote in its run file; the
request never leaves this computer. In the package (not only in tools/) so an
installed copy — which ships the package and nothing else — can stop itself.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path


def default_state_dir() -> Path:
    """Where Ninaivu keeps its state, found the way ``Config`` finds it —
    without importing ``Config``, which brings Flask in, and stopping has to
    work from whatever Python is on PATH (see tools/stop.py)."""
    env = os.environ.get("NINAIVU_STATE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base).expanduser().resolve() / "ninaivu"
    return Path.home() / ".ninaivu"


def tls_context(state_dir: Path | str | None = None) -> ssl.SSLContext:
    """How the https request checks who answered on the loopback port.

    Ninaivu signs its own certificate with a CA of its own, kept in the state
    folder as ``tls/ninaivu-ca.crt`` (``ninaivu.utils.tls``), and that
    certificate names 127.0.0.1 — so the request can be verified like any
    other, against that CA and that address, and the token goes only to the
    Ninaivu this account started. Without a CA file, the server was given a
    certificate from elsewhere (``--cert``): whose it is cannot be known
    here, and the request is made unverified, as it always was. It still
    never leaves the machine.
    """
    ca = Path(state_dir) if state_dir is not None else default_state_dir()
    ca = ca / "tls" / "ninaivu-ca.crt"
    if ca.is_file():
        return ssl.create_default_context(cafile=str(ca))
    return ssl._create_unverified_context()  # noqa: S323


def ask_to_stop(port: int, token: str, timeout: float = 10.0,
                scheme: str = "http", state_dir: Path | str | None = None) -> bool:
    """Post the token to the shutdown endpoint. True if it was accepted.

    *state_dir* is where the server's own CA is looked for (``tls_context``);
    left out, it is found as the server finds it.
    """
    if not port or port <= 0:
        return False
    schemes = [scheme] if scheme in ("http", "https") else []
    schemes += [candidate for candidate in ("http", "https")
                if candidate not in schemes]
    for candidate in schemes:
        url = f"{candidate}://127.0.0.1:{port}/api/admin/shutdown"
        request = urllib.request.Request(
            url, data=json.dumps({"token": token}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            context = tls_context(state_dir) if candidate == "https" else None
            with urllib.request.urlopen(request, timeout=timeout,
                                        context=context) as answer:
                return answer.status == 200
        except urllib.error.HTTPError:
            continue                   # wrong scheme or a rejected request
        except Exception:             # noqa: BLE001 — wrong scheme, or nothing there
            continue
    return False
