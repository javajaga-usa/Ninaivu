"""Asking a running Ninaivu to stop, the way every stop button does.

The shutdown endpoint takes the token the server wrote in its run file; the
request never leaves this computer. In the package (not only in tools/) so an
installed copy — which ships the package and nothing else — can stop itself.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def ask_to_stop(port: int, token: str, timeout: float = 10.0,
                scheme: str = "http") -> bool:
    """Post the token to the shutdown endpoint. True if it was accepted."""
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
            context = None
            if candidate == "https":
                # Ninaivu's own certificate is self-signed by design, and this
                # request never leaves the machine.
                import ssl

                context = ssl._create_unverified_context()  # noqa: S323
            with urllib.request.urlopen(request, timeout=timeout,
                                        context=context) as answer:
                return answer.status == 200
        except urllib.error.HTTPError:
            continue                   # wrong scheme or a rejected request
        except Exception:             # noqa: BLE001 — wrong scheme, or nothing there
            continue
    return False
