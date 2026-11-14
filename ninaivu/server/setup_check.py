"""The setup check: is this computer set up so the household can use Ninaivu?

One page of the console asks it (Setup check, under System). It looks at the
four things that stop a family library working more often than anything in
Ninaivu itself, and says for each what is wrong and what to do about it in
plain words:

* **Ports**: the family app and the console answer where they should, a
  taken port has not moved them somewhere phones were never told about, they
  are open to the home network, and the firewall lets phones in.
* **Drives**: every folder Ninaivu keeps something in is there (a drive
  that is not plugged in is said as that), and no disk is nearly full.
* **Tailscale**: if it is how the household reaches Ninaivu away from home,
  it is connected, the tailnet gives this computer a certificate, Ninaivu
  has fetched it, and Funnel has not put anything on the public internet.
* **Permissions**: Ninaivu can open and add to the library folders (on a Mac,
  privacy settings are the usual reason it cannot), its own folder is private
  to its account, and it is not running as root.

It only looks. Nothing is changed, nothing outside this computer is
contacted (Tailscale's own command answers from this computer), and no
secret is read. Every lookup that asks the system goes through
:class:`Probes`, so the tests can stand in for a Mac, a Windows PC or a
Linux box without being one.

Each sentence is fixed English marked with ``said`` and sent with its values
apart (``{"said": template, "params": {...}}``), so the console translates it
before filling the values in, as ``i18n.fromServer`` does for an error.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..words import filled, said

#: The order the areas are shown in, with their headings.
AREAS: tuple[tuple[str, str], ...] = (
    ("ports", said("Ports")),
    ("drives", said("Drives")),
    ("tailscale", said("Tailscale")),
    ("permissions", said("Permissions")),
)

#: What a check can say: all is well; worth a look; needs fixing; or nothing
#: to do, only to know (Tailscale not installed when nobody uses it).
STATES = ("ok", "warn", "fix", "info")

#: Below either, a disk is nearly full and Ninaivu may stop writing to it.
FULL_BYTES = 2 * 1024 ** 3
FULL_SHARE = 0.02
#: Below either, it is getting full.
LOW_BYTES = 10 * 1024 ** 3
LOW_SHARE = 0.08

#: Where Tailscale's tailnet owner turns HTTPS certificates on.
TAILSCALE_HTTPS_SETTING = "https://login.tailscale.com/admin/dns"
TAILSCALE_DOWNLOAD = "https://tailscale.com/download"

#: How long one answer is shown again rather than asked for anew: the page
#: opens, the person reads, and only "Check again" needs a fresh look.
FRESH_FOR = 30.0

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
EVERYWHERE = {"", "0.0.0.0", "::"}


def _words(template: str, **params: Any) -> dict[str, Any]:
    """A sentence for the console: the template to translate and its values."""
    values = {name: str(value) for name, value in params.items()}
    return {"said": template, "params": values, "text": filled(template, values)}


@dataclass
class Check:
    """One line of the page."""
    area: str
    key: str
    state: str
    title: dict[str, Any]
    #: What was found, when the title alone does not say it.
    detail: dict[str, Any] | None = None
    #: What to do, in plain words; None when there is nothing to do.
    fix: dict[str, Any] | None = None
    #: A command to copy, when the fix is one.
    command: str = ""
    #: The console page the fix is done on.
    page: str = ""
    #: A page outside Ninaivu the fix is done on (Tailscale's own).
    link: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"area": self.area, "key": self.key, "state": self.state,
                "title": self.title, "detail": self.detail, "fix": self.fix,
                "command": self.command, "page": self.page, "link": self.link}


# ---------------------------------------------------------------------------
# What is asked of the system
# ---------------------------------------------------------------------------

def _run(command: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _connects(host: str, port: int, timeout: float = 1.5) -> bool:
    import socket                                                  # noqa: PLC0415
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            return connection.connect_ex((host, int(port))) == 0
    except OSError:
        return False


def _lan_addresses() -> list[str]:
    try:
        from ..utils import tls                                    # noqa: PLC0415
        return list(tls.lan_addresses())
    except Exception:                                              # noqa: BLE001
        return []


def _disk_usage(path: str) -> tuple[int, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.free, usage.total


def _device(path: str) -> int | None:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _can_list(path: str) -> str:
    """"ok", "denied" (the system said no) or "error" (anything else)."""
    try:
        with os.scandir(path) as entries:
            next(entries, None)
        return "ok"
    except PermissionError:
        return "denied"
    except OSError:
        return "error"


def _can_write(path: str) -> bool:
    return os.access(path, os.W_OK)


def _mode(path: str) -> int | None:
    try:
        return os.stat(path).st_mode
    except OSError:
        return None


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def _account() -> str:
    try:
        import getpass                                             # noqa: PLC0415
        return getpass.getuser()
    except Exception:                                              # noqa: BLE001
        return ""


def _tailscale_cli() -> str | None:
    try:
        from ..utils import tailnet                                # noqa: PLC0415
        return tailnet.find_cli()
    except Exception:                                              # noqa: BLE001
        return None


def _saved_tailnet_name(state_dir: str) -> str:
    try:
        from ..utils import tailnet, tls                           # noqa: PLC0415
        return tailnet.saved_name(tls.tls_dir(state_dir)) or ""
    except Exception:                                              # noqa: BLE001
        return ""


@dataclass
class Probes:
    """Every question the check asks the system, one function each."""
    platform: str = sys.platform
    connects: Callable[[str, int], bool] = _connects
    lan_addresses: Callable[[], list[str]] = _lan_addresses
    exists: Callable[[str], bool] = os.path.isdir
    disk_usage: Callable[[str], tuple[int, int] | None] = _disk_usage
    device: Callable[[str], int | None] = _device
    can_list: Callable[[str], str] = _can_list
    can_write: Callable[[str], bool] = _can_write
    mode: Callable[[str], int | None] = _mode
    is_root: Callable[[], bool] = _is_root
    account: Callable[[], str] = _account
    run: Callable[[list[str]], Any] = _run
    tailscale_cli: Callable[[], str | None] = _tailscale_cli
    saved_tailnet_name: Callable[[str], str] = _saved_tailnet_name


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def _probe_host(host: str | None) -> str:
    """Where to knock to reach a server bound to *host* from this computer."""
    host = (host or "").strip()
    if host in EVERYWHERE or host == "localhost":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _url(scheme: str, host: str, port: int) -> str:
    default = 443 if scheme == "https" else 80
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + ("" if int(port) == default else f":{int(port)}")


def _rule_covers(output: str, port: int) -> bool:
    """Whether *port* is one of the numbers or ranges a netsh rule lists (as
    numbers: as text, 80 was "in" a rule for 8080)."""
    for match in re.finditer(r"(?<!\d)(\d+)(?:\s*-\s*(\d+))?(?!\d)", output or ""):
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) else low
        if low <= port <= high:
            return True
    return False


def check_ports(cfg: Any, probes: Probes, *, asked: dict[str, int] | None = None,
                scheme: str = "http", console_running: bool = True) -> list[Check]:
    found: list[Check] = []
    asked = asked or {}
    port = int(getattr(cfg, "port", 80) or 80)
    admin_port = int(getattr(cfg, "admin_port", 3000) or 3000)
    family_host = getattr(cfg, "host", "") or ""
    console_host = getattr(cfg, "admin_host", None) or family_host

    for key, name_port, bound, wanted in (
            ("family-port", port, family_host, asked.get("port")),
            ("console-port", admin_port, console_host, asked.get("admin_port"))):
        family = key == "family-port"
        if not family and not console_running:
            continue
        answers = probes.connects(_probe_host(bound), name_port)
        if not answers:
            found.append(Check(
                "ports", key, "fix",
                _words(said("The family app does not answer on port {port}."), port=name_port) if family
                else _words(said("The console does not answer on port {port}."), port=name_port),
                fix=_words(said("Restart Ninaivu from the Server page. If it still does not answer, the server log on the same page says why.")),  # noqa: E501
                page="server"))
        elif wanted and int(wanted) != name_port:
            found.append(Check(
                "ports", key, "warn",
                _words(said("Port {wanted} was taken, so the family app is on port {port}."), wanted=wanted, port=name_port) if family  # noqa: E501
                else _words(said("Port {wanted} was taken, so the console is on port {port}."), wanted=wanted, port=name_port),  # noqa: E501
                detail=_words(said("Another program was using port {wanted} when Ninaivu started. Addresses and bookmarks with port {wanted} in them do not reach Ninaivu now."), wanted=wanted),  # noqa: E501
                fix=_words(said("Close the other program, or turn off the setting that uses that port, then restart Ninaivu from the Server page. Until then, use the addresses on the Server page.")),  # noqa: E501
                page="server"))
        else:
            found.append(Check(
                "ports", key, "ok",
                _words(said("The family app answers on port {port}."), port=name_port) if family
                else _words(said("The console answers on port {port}."), port=name_port)))

    on_network = family_host not in LOOPBACK
    if not on_network:
        found.append(Check(
            "ports", "network", "warn",
            _words(said("Only this computer can open the family app.")),
            fix=_words(said("Turn on Network access on the Server page, so phones and laptops at home can open it too.")),  # noqa: E501
            page="server"))
        return found

    lan = [a for a in probes.lan_addresses() if a and a not in LOOPBACK]
    if not lan:
        found.append(Check(
            "ports", "network", "warn",
            _words(said("This computer is not connected to a home network.")),
            fix=_words(said("Connect it to the Wi-Fi or plug in its network cable, then check again. Phones can reach Ninaivu only through the same network, or through Tailscale.")),  # noqa: E501
        ))
    else:
        found.append(Check(
            "ports", "network", "ok",
            _words(said("Phones at home can open the family app at {url}"), url=_url(scheme, lan[0], port))))

    found.extend(_firewall(probes, port, admin_port))
    return found


def _firewall(probes: Probes, port: int, admin_port: int) -> list[Check]:
    if probes.platform == "darwin":
        return _mac_firewall(probes)
    if probes.platform == "win32":
        return _windows_firewall(probes, port)
    if probes.platform.startswith("linux"):
        return _linux_firewall(probes, port)
    return []


def _mac_firewall(probes: Probes) -> list[Check]:
    tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    state = probes.run([tool, "--getglobalstate"])
    text = (getattr(state, "stdout", "") or "") if state is not None else ""
    if not text:
        return []
    if "disabled" in text or "State = 0" in text:
        return [Check("ports", "firewall", "ok",
                      _words(said("The Mac's firewall is off, so it does not stop phones at home.")))]
    blocking = probes.run([tool, "--getblockall"])
    if blocking is not None and "enabled" in (getattr(blocking, "stdout", "") or "").lower():
        return [Check(
            "ports", "firewall", "fix",
            _words(said("The Mac's firewall blocks every incoming connection, so phones cannot reach Ninaivu.")),  # noqa: E501
            fix=_words(said("Open System Settings, then Network, then Firewall, then Options. Turn off Block all incoming connections, and set Ninaivu to Allow incoming connections.")),  # noqa: E501
        )]
    return [Check(
        "ports", "firewall", "warn",
        _words(said("The Mac's firewall is on.")),
        detail=_words(said("It lets Ninaivu in only if Ninaivu was allowed when the Mac asked.")),
        fix=_words(said("If phones at home cannot open Ninaivu, open System Settings, then Network, then Firewall, then Options, and set Ninaivu (or Python) to Allow incoming connections.")),  # noqa: E501
    )]


def _windows_firewall(probes: Probes, port: int) -> list[Check]:
    found: list[Check] = []
    profile = probes.run(["powershell", "-NoProfile", "-Command",
                          "(Get-NetConnectionProfile | Where-Object "
                          "{ $_.IPv4Connectivity -ne 'Disconnected' }).NetworkCategory"])
    if profile is not None and "Public" in (getattr(profile, "stdout", "") or ""):
        found.append(Check(
            "ports", "network-profile", "fix",
            _words(said("Windows treats this network as Public, and blocks phones on it.")),
            fix=_words(said("Open Settings, then Network & internet, then your Wi-Fi or Ethernet network, and set Network profile type to Private.")),  # noqa: E501
        ))
    rule = probes.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=Ninaivu"])
    if rule is None:
        return found
    if _rule_covers(getattr(rule, "stdout", "") or "", port):
        found.append(Check("ports", "firewall", "ok",
                           _words(said("Windows Firewall lets phones at home reach port {port}."), port=port)))
    else:
        found.append(Check(
            "ports", "firewall", "fix",
            _words(said("Windows Firewall has no rule for port {port}, so phones wait and then give up."), port=port),  # noqa: E501
            fix=_words(said("Open Terminal as administrator (right-click Start, Terminal (Admin)) and run this once:")),  # noqa: E501
            command=(f'netsh advfirewall firewall add rule name="Ninaivu" dir=in action=allow '
                     f"protocol=TCP localport={port} profile=private"),
        ))
    return found


def _linux_firewall(probes: Probes, port: int) -> list[Check]:
    # Reading ufw's rules needs root; its on/off state is in a file anybody
    # may read. firewalld says whether it runs without root too.
    ufw = probes.run(["sh", "-c", "grep -s '^ENABLED=' /etc/ufw/ufw.conf"])
    if ufw is not None and "ENABLED=yes" in (getattr(ufw, "stdout", "") or ""):
        return [Check(
            "ports", "firewall", "warn",
            _words(said("The ufw firewall is on.")),
            detail=_words(said("Ninaivu cannot read its rules without administrator rights.")),
            fix=_words(said("If phones at home cannot open Ninaivu, run this once in a terminal:")),
            command=f"sudo ufw allow {port}/tcp",
        )]
    firewalld = probes.run(["firewall-cmd", "--state"])
    if firewalld is not None and (getattr(firewalld, "stdout", "") or "").strip() == "running":
        return [Check(
            "ports", "firewall", "warn",
            _words(said("The firewalld firewall is on.")),
            detail=_words(said("Ninaivu cannot read its rules without administrator rights.")),
            fix=_words(said("If phones at home cannot open Ninaivu, run this once in a terminal:")),
            command=f"sudo firewall-cmd --permanent --add-port={port}/tcp && sudo firewall-cmd --reload",
        )]
    return []


# ---------------------------------------------------------------------------
# Drives
# ---------------------------------------------------------------------------

def _gb(size: int) -> str:
    if size >= 1024 ** 4:
        return f"{size / 1024 ** 4:.1f} TB"
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GB"
    return f"{max(size, 0) / 1024 ** 2:.0f} MB"


def _drive_of(path: str, platform: str) -> tuple[str, str]:
    """``(name, mount point)`` of the removable drive *path* is on, or
    ``("", "")`` when it is not on one Ninaivu can tell (the system disk)."""
    parts = Path(path).parts
    if platform == "darwin" and len(parts) >= 3 and parts[1] == "Volumes":
        return parts[2], str(Path(*parts[:3]))
    if platform.startswith("linux") and len(parts) >= 3:
        if parts[1] == "mnt":
            return parts[2], str(Path(*parts[:3]))
        if parts[1] == "media" and len(parts) >= 4:
            return parts[3], str(Path(*parts[:4]))
        if parts[1] == "run" and len(parts) >= 5 and parts[2] == "media":
            return parts[4], str(Path(*parts[:5]))
    if platform == "win32" and re.match(r"^[A-Za-z]:", path or "") and not path.upper().startswith("C:"):
        return path[:2].upper(), path[:2].upper() + "\\"
    return "", ""


def _roots(cfg: Any) -> list[str]:
    roots = getattr(cfg, "library_roots", None)
    if callable(roots):
        roots = roots()
    return [str(r) for r in (roots if roots is not None else getattr(cfg, "roots", []) or [])]


def places(cfg: Any) -> list[tuple[str, str, str]]:
    """``(what, path, page)`` for every folder Ninaivu keeps something in."""
    found: list[tuple[str, str, str]] = []
    roots = _roots(cfg)
    for root in roots:
        found.append((said("Library folder"), str(root), "library"))
    found.append((said("Ninaivu's own folder (index and thumbnails)"), str(cfg.state_dir), ""))
    backup_dir = str(getattr(cfg, "backup_dir", "") or "")
    if backup_dir:
        found.append((said("Copies of the index"), backup_dir, "advanced"))
    if getattr(cfg, "mirror_enabled", False) and getattr(cfg, "mirror_dir", ""):
        found.append((said("Second copy on another disk"), str(cfg.mirror_dir), "cloud"))
    if getattr(cfg, "mugil_role", "single") == "secondary" and getattr(cfg, "mugil_originals_dir", ""):
        found.append((said("Originals kept for the Primary"), str(cfg.mugil_originals_dir), "cloud"))
    seen: set[str] = set()
    unique = []
    for what, path, page in found:
        if path and path not in seen:
            seen.add(path)
            unique.append((what, path, page))
    return unique


def check_drives(cfg: Any, probes: Probes) -> list[Check]:
    found: list[Check] = []
    present: list[tuple[str, str]] = []
    for what, path, page in places(cfg):
        if probes.exists(path):
            present.append((what, path))
            continue
        drive, mount = _drive_of(path, probes.platform)
        if drive and not probes.exists(mount):
            found.append(Check(
                "drives", f"there:{path}", "fix",
                _words(said("The drive {drive} is not connected."), drive=drive),
                detail=_words(said("{what}: {path}"), what=what, path=path),
                fix=_words(said("Plug the drive in and wait for it to appear, then check again. If it has a new name, choose the folder again.")),  # noqa: E501
                page=page))
        else:
            found.append(Check(
                "drives", f"there:{path}", "fix",
                _words(said("A folder Ninaivu uses is not there.")),
                detail=_words(said("{what}: {path}"), what=what, path=path),
                fix=_words(said("If it was moved or renamed, choose it again. If it is on a drive, check the drive is plugged in.")),  # noqa: E501
                page=page))

    if present and not found:
        found.append(Check("drives", "there", "ok",
                           _words(said("Every folder Ninaivu uses is there ({count})."), count=len(present))))

    # One line per disk, not per folder: three folders on one nearly full disk
    # are one thing to fix.
    disks: dict[Any, list[tuple[str, str]]] = {}
    for what, path in present:
        disks.setdefault(probes.device(path) or path, []).append((what, path))
    for held in disks.values():
        what, path = held[0]
        usage = probes.disk_usage(path)
        if not usage:
            continue
        free, total = usage
        share = free / total if total else 1.0
        names = ", ".join(p for _, p in held)
        if free < FULL_BYTES or share < FULL_SHARE:
            found.append(Check(
                "drives", f"space:{path}", "fix",
                _words(said("A disk is nearly full: {free} free of {total}."), free=_gb(free), total=_gb(total)),
                detail=_words(said("On it: {paths}"), paths=names),
                fix=_words(said("Make room on it (empty the Trash, move other files off), or move the library to a bigger disk. Ninaivu stops adding photographs before the disk is full.")),  # noqa: E501
                page="large-files"))
        elif free < LOW_BYTES or share < LOW_SHARE:
            found.append(Check(
                "drives", f"space:{path}", "warn",
                _words(said("A disk is getting full: {free} free of {total}."), free=_gb(free), total=_gb(total)),
                detail=_words(said("On it: {paths}"), paths=names),
                fix=_words(said("Make room before the next big import. Large files shows what takes the most space.")),  # noqa: E501
                page="large-files"))
        else:
            found.append(Check(
                "drives", f"space:{path}", "ok",
                _words(said("{free} free of {total}."), free=_gb(free), total=_gb(total)),
                detail=_words(said("On it: {paths}"), paths=names)))

    # The copies of the index on the disk the index is on: one failing disk
    # takes both.
    state = str(cfg.state_dir)
    backup = str(getattr(cfg, "backup_dir", "") or "")
    if backup and probes.exists(backup) and probes.exists(state):
        same = probes.device(backup) is not None and probes.device(backup) == probes.device(state)
        if same:
            found.append(Check(
                "drives", "backup-disk", "warn",
                _words(said("The copies of the index are on the same disk as the index.")),
                fix=_words(said("Choose a folder on another disk for them (Copies of the index, under Advanced settings), so one failing disk cannot take both.")),  # noqa: E501
                page="advanced"))
    return found


# ---------------------------------------------------------------------------
# Tailscale
# ---------------------------------------------------------------------------

def _tailnet_status(probes: Probes, cli: str) -> dict[str, Any] | None:
    done = probes.run([cli, "status", "--json"])
    if done is None or getattr(done, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(getattr(done, "stdout", "") or "{}")
    except ValueError:
        return None
    me = data.get("Self") or {}
    name = str(me.get("DNSName") or "").rstrip(".").lower()
    return {"running": data.get("BackendState") == "Running", "name": name,
            "https": bool(name) and name in [str(d).lower() for d in (data.get("CertDomains") or [])],
            "state": str(data.get("BackendState") or "")}


def check_tailscale(cfg: Any, probes: Probes, *, scheme: str = "http") -> list[Check]:
    from . import remote                                           # noqa: PLC0415
    chosen = remote.chosen(cfg)
    family_on_network = (getattr(cfg, "host", "") or "") not in LOOPBACK

    if chosen in ("wireguard", "tunnel", "proxy"):
        access = remote.resolve(cfg)
        if not access.problems:
            return [Check("tailscale", "provider", "info",
                          _words(said("Away from home, the household uses {provider}, not Tailscale."), provider=access.title))]  # noqa: E501
        return [Check("tailscale", f"provider:{i}", "warn",
                      _words(said("{provider} needs a setting."), provider=access.title),
                      detail={"said": problem, "params": {}, "text": problem}, page="advanced")
                for i, problem in enumerate(access.problems)]
    if chosen == "none":
        return [Check("tailscale", "provider", "info",
                      _words(said("Opening Ninaivu away from home is turned off.")),
                      fix=_words(said("To open it away from home, install Tailscale and set Remote access to Tailscale or Automatic under Advanced settings.")),  # noqa: E501
                      page="advanced")]

    cli = probes.tailscale_cli()
    if not cli:
        state = "fix" if chosen == "tailscale" else "info"
        return [Check("tailscale", "installed", state,
                      _words(said("Tailscale is not installed on this computer.")),
                      detail=_words(said("It is needed only to open Ninaivu away from home. Without it, Ninaivu works at home as it always does.")),  # noqa: E501
                      fix=_words(said("To open Ninaivu away from home, install Tailscale on this computer and on each phone, and sign them in to the same account.")),  # noqa: E501
                      link=TAILSCALE_DOWNLOAD)]

    net = _tailnet_status(probes, cli)
    if not net or not net["running"]:
        return [Check("tailscale", "connected", "fix",
                      _words(said("Tailscale is installed but not connected.")),
                      fix=_words(said("Open Tailscale on this computer and sign in, with the account the family's phones use. Then check again.")),  # noqa: E501
                      )]
    found = [Check("tailscale", "connected", "ok",
                   _words(said("Tailscale is connected as {name}."), name=net["name"] or "?"))]

    if not family_on_network:
        found.append(Check("tailscale", "listening", "fix",
                           _words(said("Tailscale is connected, but Ninaivu listens only on this computer.")),
                           fix=_words(said("Turn on Network access on the Server page, so devices on the tailnet can open it.")),  # noqa: E501
                           page="server"))

    if getattr(cfg, "tailnet_https", True):
        saved = probes.saved_tailnet_name(str(cfg.state_dir))
        if not net["https"]:
            found.append(Check("tailscale", "certificate", "warn",
                               _words(said("HTTPS certificates are off for your tailnet.")),
                               detail=_words(said("Phones then warn that the connection is not private.")),
                               fix=_words(said("The tailnet's owner turns on HTTPS Certificates on the DNS page of Tailscale's admin console. Then restart Ninaivu from the Server page.")),  # noqa: E501
                               link=TAILSCALE_HTTPS_SETTING))
        elif saved.lower() != net["name"]:
            found.append(Check("tailscale", "certificate", "warn",
                               _words(said("Ninaivu has not fetched Tailscale's certificate yet.")),
                               fix=_words(said("Restart Ninaivu from the Server page. It fetches the certificate when it starts, and again every six hours.")),  # noqa: E501
                               page="server"))
        elif family_on_network:
            found.append(Check("tailscale", "certificate", "ok",
                               _words(said("Away from home, the family app is at {url}"),
                                      url=_url("https" if scheme == "https" else scheme, net["name"],
                                               int(getattr(cfg, "port", 80) or 80)))))

    served = probes.run([cli, "funnel", "status"])
    text = ((getattr(served, "stdout", "") or "") + (getattr(served, "stderr", "") or "")) if served else ""
    if "Funnel on" in text:
        found.append(Check("tailscale", "funnel", "fix",
                           _words(said("Tailscale Funnel is on: something on this computer is on the public internet.")),  # noqa: E501
                           detail=_words(said("Ninaivu never needs Funnel. Only devices signed in to your tailnet should reach it.")),  # noqa: E501
                           fix=_words(said("Unless you turned it on for something else on purpose, turn it off with:")),
                           command=f'"{cli}" funnel reset' if " " in cli else f"{cli} funnel reset"))
    return found


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def _denied_fix(platform: str, account: str) -> dict[str, Any]:
    if platform == "darwin":
        return _words(said("Open System Settings, then Privacy & Security, then Full Disk Access. Turn on Ninaivu (or Terminal or Python, whichever starts it), then restart Ninaivu from the Server page."))  # noqa: E501
    if platform == "win32":
        return _words(said("Right-click the folder, choose Properties, then Security, and give {account} permission to read and write it."), account=account or "?")  # noqa: E501
    return _words(said("Give the account {account} permission to read and write the folder, for example with chown or chmod, then check again."), account=account or "?")  # noqa: E501


def check_permissions(cfg: Any, probes: Probes) -> list[Check]:
    found: list[Check] = []
    account = probes.account()
    roots = _roots(cfg)
    readable = 0
    for root in roots:
        root = str(root)
        if not probes.exists(root):
            continue                       # the Drives area says so
        listed = probes.can_list(root)
        if listed != "ok":
            fix = _denied_fix(probes.platform, account)
            found.append(Check(
                "permissions", f"read:{root}", "fix",
                _words(said("Ninaivu is not allowed to open {path}."), path=root),
                detail=_words(said("macOS keeps apps out of some folders and drives until they are allowed.")) if probes.platform == "darwin" else None,  # noqa: E501
                fix=fix))
            continue
        readable += 1
        if not probes.can_write(root):
            fix = _denied_fix(probes.platform, account)
            found.append(Check(
                "permissions", f"write:{root}", "warn",
                _words(said("Ninaivu can read {path} but cannot add to it."), path=root),
                detail=_words(said("Photographs from phones, uploads and Import are put in the library, so they cannot be saved here.")),  # noqa: E501
                fix=fix))
    if readable and not any(c.key.startswith(("read:", "write:")) for c in found):
        found.append(Check("permissions", "library", "ok",
                           _words(said("Ninaivu can open and add to every library folder."))))

    state = str(cfg.state_dir)
    if probes.exists(state):
        if not probes.can_write(state):
            found.append(Check(
                "permissions", "state-write", "fix",
                _words(said("Ninaivu cannot write to its own folder, {path}."), path=state),
                detail=_words(said("The index, thumbnails and settings are kept there.")),
                fix=_words(said("Give the account {account} permission to write to it, then restart Ninaivu."), account=account or "?")))  # noqa: E501
        mode = probes.mode(state)
        if probes.platform != "win32" and mode is not None and mode & 0o077:
            found.append(Check(
                "permissions", "state-private", "warn",
                _words(said("Other accounts on this computer can open Ninaivu's own folder.")),
                detail=_words(said("It holds the index, the faces and the sign-in sessions.")),
                fix=_words(said("Make it private to this account with:")),
                command=f'chmod 700 "{state}"'))
        elif probes.platform != "win32" and mode is not None:
            found.append(Check("permissions", "state-private", "ok",
                               _words(said("Ninaivu's own folder is private to its account."))))

    if probes.is_root():
        found.append(Check(
            "permissions", "root", "warn",
            _words(said("Ninaivu is running as root, the system's own administrator.")),
            detail=_words(said("Then a mistake in Ninaivu, or in anything it opens, could change any file on this computer.")),  # noqa: E501
            fix=_words(said("Start it as an ordinary account instead, the one that owns the library folders.")),
        ))
    return found


# ---------------------------------------------------------------------------
# The whole page
# ---------------------------------------------------------------------------

def run_all(cfg: Any, probes: Probes | None = None, *, asked: dict[str, int] | None = None,
            scheme: str = "http", console_running: bool = True) -> dict[str, Any]:
    """Every check, grouped by area, with a count of each state."""
    probes = probes or Probes()
    checks: list[Check] = []
    for area, look in (
            ("ports", lambda: check_ports(cfg, probes, asked=asked, scheme=scheme,
                                          console_running=console_running)),
            ("drives", lambda: check_drives(cfg, probes)),
            ("tailscale", lambda: check_tailscale(cfg, probes, scheme=scheme)),
            ("permissions", lambda: check_permissions(cfg, probes))):
        try:
            checks.extend(look())
        except Exception as exc:                                   # noqa: BLE001
            # One area that cannot be looked at must not hide the others.
            checks.append(Check(area, "error", "warn",
                                _words(said("This could not be checked.")),
                                detail={"said": str(exc), "params": {}, "text": str(exc)}))
    counts = {state: sum(1 for c in checks if c.state == state) for state in STATES}
    return {
        "checked_at": time.time(),
        "areas": [{"key": key, "title": _words(title)} for key, title in AREAS],
        "checks": [c.as_dict() for c in checks],
        "counts": counts,
    }


_held: dict[str, Any] = {}
_held_lock = threading.Lock()


def cached(cfg: Any, *, fresh: bool = False, **kwargs: Any) -> dict[str, Any]:
    """:func:`run_all`, or its answer from the last :data:`FRESH_FOR` seconds."""
    now = time.monotonic()
    with _held_lock:
        if not fresh and _held and now - _held["at"] < FRESH_FOR and _held["cfg"] is cfg:
            return _held["value"]
    value = run_all(cfg, **kwargs)
    with _held_lock:
        _held.update(at=now, cfg=cfg, value=value)
    return value


__all__ = ["AREAS", "Check", "Probes", "check_drives", "check_permissions", "check_ports",
           "check_tailscale", "cached", "places", "run_all"]

