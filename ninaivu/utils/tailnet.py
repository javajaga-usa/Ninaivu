"""Answering on this computer's Tailscale name, with Tailscale's certificate.

Ninaivu listens on every network the computer is on, the tailnet included, so a
phone signed in to the household's tailnet already reaches it at
``https://<computer>.<tailnet>.ts.net`` (the family app) and ``:3000`` (the
console), from anywhere, and nobody outside the tailnet can. What it lacked was
a certificate for that name: Ninaivu's own is for the house's names (its CA is
constrained to them), so a phone refused the connection.

Tailscale gives each computer a Let's Encrypt certificate for its ts.net name
once the tailnet owner turns HTTPS certificates on. This fetches it from the
Tailscale on this computer (``tailscale cert``, written to standard output:
the Mac app's command line is sandboxed and cannot write files where it is
asked), keeps a copy in the state folder, and presents it to a browser that
asks for that name. Every other name gets Ninaivu's own certificate, as before.
It is fetched again every few hours, so Tailscale's renewals are picked up
without a restart.

Tailscale Serve would do the same job as a proxy, but on a Mac it cannot take
port 443 while Ninaivu holds it: the app runs as the user, and macOS lets a user
bind a port below 1024 only on every address, which Ninaivu already has.

Nothing here is required. Without Tailscale, with HTTPS certificates off, or
with anything going wrong, Ninaivu answers exactly as it did. The certificate
choice is made during the TLS handshake and must never fail one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Where the command line is when it is not on the PATH.
CANDIDATES = {
    "darwin": ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
               "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"],
    "win32": [r"C:\Program Files\Tailscale\tailscale.exe"],
}
#: How often the certificate is fetched again. Tailscale renews it a month
#: before its 90 days are up, so this only has to notice within days.
REFRESH_SECONDS = 6 * 3600

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(command: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(command, capture_output=True, text=True, timeout=120)


def find_cli(platform: str | None = None, which: Callable[[str], str | None] = shutil.which,
             exists: Callable[[str], bool] = os.path.isfile) -> str | None:
    found = which("tailscale")
    if found:
        return found
    for path in CANDIDATES.get(platform or sys.platform, []):
        if exists(path):
            return path
    return None


def this_computer(cli: str, runner: Runner = _run) -> dict[str, Any] | None:
    """This computer's tailnet name, and whether the tailnet gives it HTTPS
    certificates; None when Tailscale is not connected."""
    done = runner([cli, "status", "--json"])
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return None
    if data.get("BackendState") != "Running":
        return None
    name = str((data.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if not name:
        return None
    domains = [str(d).lower() for d in (data.get("CertDomains") or [])]
    return {"name": name, "https": name in domains}


_PEM = re.compile(r"-----BEGIN ([A-Z ]+)-----.*?-----END \1-----\n?", re.S)


def fetch(cli: str, name: str, runner: Runner = _run) -> tuple[str, str] | None:
    """The certificate chain and key for *name*, as PEM text; None if refused."""
    done = runner([cli, "cert", "--cert-file=-", "--key-file=-", name])
    if done.returncode != 0:
        log.info("Tailscale would not give a certificate for %s: %s", name,
                 (done.stderr or done.stdout).strip()[:300])
        return None
    chain = "".join(m.group(0) for m in _PEM.finditer(done.stdout)
                    if m.group(1) == "CERTIFICATE")
    key = "".join(m.group(0) for m in _PEM.finditer(done.stdout)
                  if "PRIVATE KEY" in m.group(1))
    return (chain, key) if chain and key else None


def saved_name(directory: Path | str) -> str:
    """The tailnet name Ninaivu answers with Tailscale's certificate, from the
    copy it keeps; '' when it has none."""
    directory = Path(directory)
    try:
        if not (directory / "tailnet.crt").is_file():
            return ""
        return str(json.loads((directory / "tailnet.json").read_text(encoding="utf-8"))["name"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


class TailnetCertificate:
    """The ts.net name, and the TLS context that answers for it."""

    def __init__(self, directory: Path | str, *, runner: Runner = _run,
                 cli: Callable[[], str | None] = find_cli) -> None:
        self.directory = Path(directory)
        self.name = ""
        self.context: ssl.SSLContext | None = None
        self._runner = runner
        self._cli = cli
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def files(self) -> tuple[Path, Path, Path]:
        return (self.directory / "tailnet.crt", self.directory / "tailnet.key",
                self.directory / "tailnet.json")

    def _load(self, name: str) -> bool:
        cert, key, _ = self.files
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.name, self.context = name, context
        return True

    def load_saved(self) -> bool:
        """The copy from last time, so the first visitor after a start does not
        wait for Tailscale to be asked."""
        _, _, meta = self.files
        try:
            name = json.loads(meta.read_text(encoding="utf-8"))["name"]
            return self._load(str(name))
        except (OSError, ValueError, KeyError, TypeError, ssl.SSLError):
            return False

    def refresh(self) -> bool:
        """Ask Tailscale for the certificate again. True if one is in use."""
        cli = self._cli()
        me = this_computer(cli, self._runner) if cli else None
        if not me or not me["https"]:
            if self.context is not None and me is not None:
                log.info("HTTPS certificates are off for the tailnet now; %s is "
                         "answered with Ninaivu's own certificate", self.name)
                self.name, self.context = "", None
            return self.context is not None
        got = fetch(cli, me["name"], self._runner)
        if got is None:
            return self.context is not None
        chain, key = got
        cert_path, key_path, meta = self.files
        self.directory.mkdir(parents=True, exist_ok=True)
        before = cert_path.read_text(encoding="utf-8") if cert_path.is_file() else ""
        _write_private(key_path, key)
        cert_path.write_text(chain, encoding="utf-8")
        meta.write_text(json.dumps({"name": me["name"]}), encoding="utf-8")
        try:
            self._load(me["name"])
        except (OSError, ssl.SSLError) as exc:
            log.warning("could not use Tailscale's certificate for %s: %s", me["name"], exc)
            return self.context is not None
        if chain != before:
            log.info("answering https://%s with Tailscale's certificate", me["name"])
        return True

    def choose(self, sock: ssl.SSLObject, server_name: str | None, _context: Any) -> None:
        """The handshake's SNI callback: Tailscale's certificate for its name."""
        try:
            if server_name and self.context is not None \
                    and server_name.lower().rstrip(".") == self.name:
                sock.context = self.context
        except Exception:                            # noqa: BLE001 - never fail a handshake
            pass
        return None

    def attach(self, context: ssl.SSLContext) -> ssl.SSLContext:
        context.sni_callback = self.choose
        return context

    def watch(self, every: float = REFRESH_SECONDS) -> None:
        """Fetch now, off the start-up path, and again every *every* seconds."""
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                try:
                    self.refresh()
                except Exception:                    # noqa: BLE001 - try again later
                    log.debug("could not refresh the tailnet certificate", exc_info=True)
                if self._stop.wait(every):
                    return

        self._thread = threading.Thread(target=loop, name="ninaivu-tailnet-cert", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _write_private(path: Path, text: str) -> None:
    """Written readable only by this account, as Ninaivu's own key is."""
    partial = path.with_name(path.name + ".part")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(partial, path)
