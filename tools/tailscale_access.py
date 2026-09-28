#!/usr/bin/env python3
"""Reach Ninaivu from outside the house, safely, through Tailscale.

    python tools/tailscale_access.py status
    python tools/tailscale_access.py enable

Tailscale puts this computer and the household's phones and laptops on one
private network (a tailnet), wherever each of them is. Ninaivu listens on every
network the computer is on, the tailnet included, so on the tailnet it is at

    family app     https://<computer>.<tailnet>.ts.net
    admin console  https://<computer>.<tailnet>.ts.net:<admin port>

and only devices signed in to the tailnet can connect at all; to everybody else
on the internet there is nothing there. Ninaivu answers that name with the
certificate Tailscale gives it (ninaivu/utils/tailnet.py), so a phone needs no
certificate installed.

``enable`` checks what that needs and that it works: Tailscale connected, HTTPS
certificates on for the tailnet (one switch in Tailscale's admin console, the
tailnet owner's), and each address answering with a certificate a phone
accepts. It also takes off any Tailscale Serve entries for Ninaivu's ports,
which an earlier version of this tool made: on a Mac, Serve cannot take port
443 while Ninaivu holds it (the app runs as the user, and macOS lets a user bind
a port below 1024 only on every address), so the family app answered with the
wrong certificate. It never uses Funnel, which would put Ninaivu on the public
internet for anybody, and ``status`` warns if Funnel is on.

Who can reach Ninaivu this way is who is in the tailnet: the household's own
devices, and anybody the Ninaivu computer is shared with. Tailscale's access
rules can narrow that, for example to keep the console to one person.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

#: Where Tailscale's own admin console turns HTTPS certificates on.
HTTPS_SETTING = "https://login.tailscale.com/admin/dns"
#: Tailnet ports an earlier version of this tool served Ninaivu on.
SERVED_BEFORE = (443, 8443)

CANDIDATES = {
    "darwin": ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
               "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"],
    "win32": [r"C:\Program Files\Tailscale\tailscale.exe"],
}

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run(command: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(command, capture_output=True, text=True, timeout=120)


def find_cli(platform: str | None = None, which: Callable[[str], str | None] = shutil.which,
             exists: Callable[[str], bool] = os.path.isfile) -> str | None:
    """The tailscale command. On a Mac the app carries it, off the PATH."""
    found = which("tailscale")
    if found:
        return found
    for path in CANDIDATES.get(platform or sys.platform, []):
        if exists(path):
            return path
    return None


def state_dir() -> Path:
    env = os.environ.get("NINAIVU_STATE_DIR")
    return Path(env).expanduser() if env else Path.home() / ".ninaivu"


def ninaivu_ports(directory: Path | None = None) -> dict[str, Any] | None:
    """Where the running Ninaivu listens, from the run file it leaves."""
    try:
        data = json.loads(((directory or state_dir()) / "ninaivu.run").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("port"):
        return None
    return {"port": int(data["port"]), "admin_port": int(data.get("admin_port") or 0),
            "scheme": str(data.get("scheme") or "http")}


def tailnet(cli: str, runner: Runner = run) -> dict[str, Any]:
    """This computer on the tailnet: its name, whether it is up, and whether the
    tailnet gives it HTTPS certificates."""
    done = runner([cli, "status", "--json"])
    if done.returncode != 0:
        return {"running": False, "error": (done.stderr or done.stdout).strip()}
    data = json.loads(done.stdout or "{}")
    me = data.get("Self") or {}
    name = str(me.get("DNSName") or "").rstrip(".")
    return {
        "running": data.get("BackendState") == "Running",
        "name": name,
        "https": bool(name) and name in (data.get("CertDomains") or []),
        "address": (me.get("TailscaleIPs") or [""])[0],
    }


def addresses(name: str, ports: dict[str, Any]) -> list[tuple[str, str]]:
    """``(what, address)`` for the family app and the console on the tailnet."""
    def url(port: int) -> str:
        return f"https://{name}" + ("" if port == 443 else f":{port}")
    found = [("family app", url(ports["port"]))]
    if ports.get("admin_port"):
        found.append(("admin console", url(ports["admin_port"])))
    return found


def answers(address: str, patience: float = 20.0, sleep: Callable[[float], None] = time.sleep,
            opener: Callable[..., Any] = urllib.request.urlopen) -> str:
    """What the address answers with, as a phone on the tailnet sees it: the
    certificate verified."""
    deadline = time.monotonic() + patience
    context = ssl.create_default_context()
    while True:
        try:
            with opener(address, timeout=15, context=context) as response:
                return f"{response.status}"
        except urllib.error.HTTPError as exc:
            return f"{exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc))
            if "CERTIFICATE_VERIFY_FAILED" in reason or time.monotonic() >= deadline:
                return f"no ({reason})"
            sleep(2)


def take_off_serve(cli: str, ports: dict[str, Any], runner: Runner = run) -> list[int]:
    """Remove Serve entries this tool used to make for Ninaivu, and only those:
    one on 443 or 8443 that proxies to Ninaivu's own ports. Returns the ports."""
    done = runner([cli, "serve", "status", "--json"])
    try:
        served = json.loads(done.stdout or "{}") if done.returncode == 0 else {}
    except ValueError:
        served = {}
    ours = {f"127.0.0.1:{ports['port']}", f"127.0.0.1:{ports.get('admin_port')}"}
    removed = []
    for where, web in (served.get("Web") or {}).items():
        port = int(str(where).rsplit(":", 1)[-1] or 0)
        targets = {str(h.get("Proxy", "")).split("://", 1)[-1]
                   for h in (web.get("Handlers") or {}).values()}
        if port in SERVED_BEFORE and targets and targets <= ours:
            runner([cli, "serve", f"--https={port}", "off"])
            removed.append(port)
    return sorted(removed)


def enable(cli: str, ports: dict[str, Any], net: dict[str, Any], *, runner: Runner = run,
           check: Callable[[str], str] = answers) -> int:
    if not net.get("running"):
        print("Tailscale is not connected on this computer. Open Tailscale and sign in first.")
        return 2
    if not net.get("https"):
        print("HTTPS certificates are not on for this tailnet, so Tailscale cannot give "
              f"{net.get('name') or 'this computer'} a certificate. Nothing was changed.\n"
              f"Turn them on at {HTTPS_SETTING} (HTTPS Certificates, Enable), then run "
              "this again.")
        return 2
    removed = take_off_serve(cli, ports, runner)
    if removed:
        print(f"  took off Tailscale Serve on {', '.join(map(str, removed))}: Ninaivu answers "
              "the tailnet itself")
    failing = 0
    for what, address in addresses(net["name"], ports):
        result = check(address)
        failing += not result.isdigit()
        print(f"  {what:14} {address}   answers: {result}")
    if failing:
        print("\nNinaivu fetches Tailscale's certificate when it starts and every six hours. "
              "Restart Ninaivu to have it now, then run this again.")
        return 1
    print("\nOnly devices signed in to your tailnet can open these. Nothing is on the "
          "public internet.")
    return 0


def status(cli: str, ports: dict[str, Any] | None, net: dict[str, Any], *,
           runner: Runner = run, check: Callable[[str], str] = answers) -> int:
    print(f"  Tailscale     {'connected' if net.get('running') else 'not connected'}"
          f"{'  as ' + net['name'] if net.get('name') else ''}")
    print(f"  HTTPS certs   {'on' if net.get('https') else 'off  (turn on at ' + HTTPS_SETTING + ')'}")
    if not ports:
        print("  Ninaivu        not running")
    elif net.get("running") and net.get("name"):
        for what, address in addresses(net["name"], ports):
            print(f"  {what:14}{address}   answers: {check(address)}")
    served = runner([cli, "serve", "status"])
    text = ((served.stdout or "") + (served.stderr or "")).strip()
    if text and text != "No serve config":
        print("\n  Tailscale Serve:\n" + text)
    if "Funnel on" in text:
        print("\n  WARNING: Funnel is on, which puts something on the public internet.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("status", "enable"))
    args = parser.parse_args(argv)

    cli = find_cli()
    if cli is None:
        print("Tailscale is not installed on this computer: https://tailscale.com/download")
        return 2
    net = tailnet(cli)
    ports = ninaivu_ports()
    if args.action == "status":
        return status(cli, ports, net)
    if ports is None:
        print("Ninaivu is not running. Start it first.")
        return 2
    return enable(cli, ports, net)


if __name__ == "__main__":
    sys.exit(main())
