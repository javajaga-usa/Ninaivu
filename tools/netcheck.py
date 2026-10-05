#!/usr/bin/env python3
"""Why can't the other devices reach Ninaivu?

Run this on the machine Ninaivu is running on:

    python tools/netcheck.py              (or --name NAME if Ninaivu uses one)

It answers the four questions that account for almost every case, in order:
is Ninaivu listening on the network at all, which address should people type,
does its name answer, and is something between here and the phone dropping
the connection.

It asks the running Ninaivu where it is, from the run file it leaves in its
state folder, rather than assuming a port. It used to assume 5000, and on a Mac
port 5000 is AirPlay Receiver: it reported "the family app is answering on port
5000" and told phones to use an address that was not Ninaivu at all.

Standard library only, so it runs before any of Ninaivu's dependencies exist.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

GREEN, RED, AMBER, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if sys.platform == "win32" and not sys.stdout.isatty():
    GREEN = RED = AMBER = DIM = OFF = ""


def say(mark: str, colour: str, text: str) -> None:
    try:
        print(f"  {colour}{mark}{OFF} {text}")
    except UnicodeEncodeError:
        ascii_mark = "OK" if mark == "✓" else ("X" if mark == "✗" else mark)
        print(f"  {colour}[{ascii_mark}]{OFF} {text}")


def ok(text: str) -> None:
    say("✓", GREEN, text)


def bad(text: str) -> None:
    say("✗", RED, text)


def warn(text: str) -> None:
    say("!", AMBER, text)


def note(text: str) -> None:
    print(f"    {DIM}{text}{OFF}")


# ---------------------------------------------------------------------------

def reachable(address: str, port: int, timeout: float = 1.5) -> bool:
    """Can a TCP connection actually be made? The only question that matters.

    An earlier version of this script tried to *bind* the port to work out what
    was listening. That is wrong on Linux: with something already on
    127.0.0.1:PORT, binding 0.0.0.0:PORT fails too, so a server listening on
    loopback alone looked like one listening everywhere — the exact fault this
    script exists to catch. Connecting asks the real question.
    """
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        return connection.connect_ex((address, port)) == 0
    finally:
        connection.close()


def state_dir() -> Path:
    """Ninaivu's state folder, found the way ninaivu/server/config.py finds it."""
    env = os.environ.get("NINAIVU_STATE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base).expanduser().resolve() / "ninaivu"
    return Path.home() / ".ninaivu"


def running() -> dict | None:
    """What the running Ninaivu wrote about itself: ports, scheme, host."""
    try:
        return json.loads((state_dir() / "ninaivu.run").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def url(host: str, port: int, scheme: str) -> str:
    """No port when it is the scheme's own: https://ninaivu.local, not :443."""
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + ("" if int(port) == default else f":{port}")


def _name(data: bytes, i: int) -> tuple[str, int]:
    """A DNS name at *i*, following compression; (name, where it ended)."""
    labels, end = [], None
    for _ in range(64):
        length = data[i]
        if length == 0:
            return ".".join(labels), (end if end is not None else i + 1)
        if length & 0xC0 == 0xC0:
            end = end if end is not None else i + 2
            i = ((length & 0x3F) << 8) | data[i + 1]
            continue
        labels.append(data[i + 1:i + 1 + length].decode("utf-8", "replace"))
        i += 1 + length
    raise ValueError("a name that does not end")


def addresses_in(data: bytes, wanted: str) -> list[str]:
    """The IPv4 addresses a DNS reply gives for *wanted*; [] for anything else."""
    found: list[str] = []
    try:
        qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
        i = 12
        for _ in range(qd):
            i = _name(data, i)[1] + 4
        for _ in range(an + ns + ar):
            owner, i = _name(data, i)
            rtype, _cls, _ttl, length = struct.unpack(">HHIH", data[i:i + 10])
            i += 10
            if rtype == 1 and length == 4 and owner.lower() == wanted.lower():
                found.append(socket.inet_ntoa(data[i:i + 4]))
            i += length
    except (ValueError, IndexError, struct.error):
        pass
    return found


def mdns_lookup(name: str, via: str | None, timeout: float = 2.5) -> list[str]:
    """Ask the network for *name* the way a phone does, and return the answers.

    A one-shot ("legacy") mDNS question from an ordinary port, to the mDNS
    group, sent out of the home adapter. Every responder on the network answers
    it by unicast, so what comes back is what a phone would be told.
    """
    wanted = name.rstrip(".").lower()
    question = b"".join(bytes([len(p)]) + p.encode() for p in wanted.split(".")) + b"\0"
    packet = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + question + struct.pack(">HH", 1, 1)
    found: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if via:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(via))
        sock.settimeout(0.5)
        sock.sendto(packet, ("224.0.0.251", 5353))
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                data, _ = sock.recvfrom(9000)
            except socket.timeout:
                continue
            for address in addresses_in(data, wanted):
                if address not in found:
                    found.append(address)
    except OSError:
        pass
    finally:
        sock.close()
    return found


def hosts_file_entry(name: str) -> str | None:
    """The address this computer's hosts file gives *name*, if it names it."""
    hosts = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts"
             if sys.platform == "win32" else Path("/etc/hosts"))
    try:
        for line in hosts.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) > 1 and name.lower() in (p.lower() for p in parts[1:]):
                return parts[0]
    except OSError:
        pass
    return None


def mac_firewall() -> str | None:
    """"off", "on" or "block-all" for the macOS firewall; None elsewhere."""
    if sys.platform != "darwin":
        return None
    tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    try:
        state = subprocess.run([tool, "--getglobalstate"], capture_output=True,
                               text=True, timeout=10, check=False).stdout
        if "disabled" in state:
            return "off"
        blocking = subprocess.run([tool, "--getblockall"], capture_output=True,
                                  text=True, timeout=10, check=False).stdout
        return "block-all" if "enabled" in blocking else "on"
    except (OSError, subprocess.SubprocessError):
        return None


def firewall_rule_exists(port: int) -> bool | None:
    """Is there an inbound rule for Ninaivu's port? None if we cannot tell."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=Ninaivu"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return str(port) in (result.stdout or "")


def mdns_firewall_rule_exists() -> bool | None:
    """Is there an inbound rule for mDNS discovery (UDP 5353)? None if we cannot tell."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=Ninaivu mDNS"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return "5353" in (result.stdout or "")


def network_is_public() -> bool | None:
    """Windows only. A private-profile rule does nothing on a public network."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-NetConnectionProfile | Where-Object "
             "{ $_.IPv4Connectivity -ne 'Disconnected' }).NetworkCategory"],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    return "Public" in text


def firewall_hint(port: int, admin: int) -> None:
    if sys.platform == "win32":
        note("Windows, in order:")
        note("")
        note("a) Settings → Network & Internet → Wi-Fi → your network, and set")
        note("   it to PRIVATE. A private-profile rule does nothing on a network")
        note("   Windows considers public, which is an easy hour to lose.")
        note("")
        note("b) Open PowerShell AS ADMINISTRATOR — press Win, type powershell,")
        note("   right-click 'Windows PowerShell' → Run as administrator. The")
        note("   title bar must say 'Administrator:'. Without that the next")
        note("   command fails with 'Access is denied' (System Error 5).")
        note("")
        note("c) Paste this. You do not need to cd anywhere first:")
        note('   New-NetFirewallRule -DisplayName "Ninaivu" -Direction Inbound '
             f"-Protocol TCP -LocalPort {port},{admin} -Action Allow -Profile Private")
        note('   New-NetFirewallRule -DisplayName "Ninaivu mDNS" -Direction Inbound '
             "-Protocol UDP -LocalPort 5353 -Action Allow -Profile Private")
    elif sys.platform == "darwin":
        note("macOS: System Settings → Network → Firewall → Options, and allow")
        note("incoming connections for Python.")
    else:
        note(f"Linux: sudo ufw allow {port}/tcp && sudo ufw allow {admin}/tcp")
        note(f"       (or: sudo firewall-cmd --add-port={port}/tcp --permanent)")


def which_address() -> tuple[str | None, list[dict[str, str]]]:
    """The address a phone should use, and every adapter, from the OS itself."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import netinfo

        return netinfo.best_address(), netinfo.adapters()
    except Exception:                            # noqa: BLE001
        return None, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", default="ninaivu",
                        help="the name Ninaivu answers to on the network (default: ninaivu)")
    args = parser.parse_args(argv)
    name = f"{args.name.strip().lower().removesuffix('.local')}.local"

    print("\n  Ninaivu network check — run this on the machine serving the library\n")

    # 1 -- is it running at all, and where?
    print("  1. Is Ninaivu running?")
    run = running()
    if not run:
        bad("Ninaivu is not running on this computer")
        note(f"(it leaves ninaivu.run in {state_dir()} while it runs). Start it first.")
        return 1
    port, admin = int(run.get("port") or 0), int(run.get("admin_port") or 0)
    scheme = run.get("scheme") or "http"
    if not reachable("127.0.0.1", port):
        bad(f"Ninaivu says it is on port {port}, but nothing answers there")
        note("It may be starting, or stopping. Give it a minute and run this again.")
        return 1
    ok(f"the family app is answering on port {port} ({scheme})")
    if admin and reachable("127.0.0.1", admin):
        ok(f"the admin console is answering on port {admin}")
    else:
        warn("no admin console — started with --no-admin?")
    if run.get("host") in ("127.0.0.1", "localhost", "::1"):
        bad("Ninaivu is serving this computer only, so no other device can reach it")
        note("Turn on Network access in the console: Server → Network access.")
        return 1

    # 2 -- which address should people type?
    print("\n  2. Which address should the phones use?")
    best, rows = which_address()
    if rows:
        width = max(len(r["name"]) for r in rows)
        for row in rows:
            if row["gateway"]:
                ok(f"{row['name']:<{width}}  {row['address']:<15} ← on your home network")
            else:
                note(f"{row['name']:<{width}}  {row['address']:<15} "
                     f"(no gateway — a virtual adapter, not reachable from a phone)")
        print()

    try:
        from ninaivu.utils import tls

        candidates = tls.lan_addresses()
    except Exception:                            # running before install
        candidates = [best] if best else []

    if best:
        ok(f"open this on the phone:  {url(best, port, scheme)}")
        if not reachable(best, port):
            bad("…but Ninaivu is not answering on it.")
            note("Restart Ninaivu. It serves the network by default, so no flags")
            note("are needed — unless it was started with --local-only.")
            return 1
    elif candidates:
        warn(f"could not tell which adapter is the home one; try "
             f"{url(candidates[0], port, scheme)}")
    else:
        bad("this machine has no network address — is it connected to Wi-Fi?")
        return 1
    if scheme == "https":
        note(f"Each device trusts Ninaivu's certificate once: {url(best or 'ADDRESS', port, scheme)}/cert")

    virtual = [r for r in rows if not r["gateway"]]
    if virtual:
        note(f"{len(virtual)} virtual adapter(s) present (Hyper-V, WSL, VirtualBox,")
        note("Docker or a VPN). Their addresses look just like home ones, and a")
        note("phone told to use one waits until it times out — which is exactly")
        note("what a firewall block looks like. Use the address marked above.")

    # 3 -- the name, which is what "works by address but not by name" means
    print(f"\n  3. Does {name} answer?")
    answers = mdns_lookup(name, best)
    if best and best in answers:
        ok(f"{name} answers with {best}, so devices can use {url(name, port, scheme)}")
        others = [a for a in answers if a != best]
        if others:
            warn(f"…but {', '.join(others)} answers for it too — another computer")
            note("claims the same name, and a phone may get either. Stop Ninaivu there,")
            note("or start one of them with --name to give it a name of its own.")
    elif answers:
        bad(f"{name} answers with {', '.join(answers)}, not with this computer ({best})")
        note("Another computer claims the name — often the one Ninaivu ran on before.")
        note("Stop Ninaivu there, or start this one with --name to use another name.")
    else:
        bad(f"nothing on the network answers for {name}")
        note("Ninaivu announces the name unless it was started with --no-mdns, and")
        note("only while serving the network. A firewall blocking UDP 5353 stops it.")
    pinned = hosts_file_entry(name)
    if pinned and pinned != best:
        note(f"This computer's hosts file sends {name} to {pinned}. That changes the")
        note("name for this computer only — but an old computer with that line,")
        note("added by Ninaivu's Windows setup, cannot reach Ninaivu by name any more:")
        note("delete it from that computer's hosts file.")
    own = socket.gethostname().split(".")[0] + ".local"
    if own.lower() != name.lower() and best and best in mdns_lookup(own, best, timeout=1.5):
        note(f"This computer also answers as {own}, announced by the system itself:")
        note(f"   {url(own, port, scheme)}")
        note("If it works on a device where the name above does not, the device is")
        note("holding an old answer: turn its Wi-Fi off and on. If neither name works")
        note("there but the address does, the router is not passing names between")
        note("devices (often between its 2.4 GHz and 5 GHz networks).")

    # 4 -- the firewall, which is what a timeout usually means
    print("\n  4. Is anything blocking the connection?")
    rule = firewall_rule_exists(port)
    mdns_rule = mdns_firewall_rule_exists()
    public = network_is_public()
    mac = mac_firewall()

    if rule is False:
        bad(f"no Windows Firewall rule lets devices reach port {port}.")
        note("This is the usual cause of 'it loads forever and times out':")
        note("Windows DROPS blocked connections rather than refusing them, so")
        note("the phone gets no error at all — it just waits.")
        note("")
        firewall_hint(port, admin)
        return 1
    if rule is True:
        ok(f"a Windows Firewall rule lets devices reach port {port}")

    if mdns_rule is False:
        warn("no firewall rule for mDNS (UDP 5353) exists.")
        note(f"Phones and other devices will NOT be able to resolve '{name}',")
        note("even though accessing by raw IP address works. In PowerShell,")
        note("run as administrator:")
        note('   New-NetFirewallRule -DisplayName "Ninaivu mDNS" -Direction Inbound '
             "-Protocol UDP -LocalPort 5353 -Action Allow -Profile Private")
    elif mdns_rule is True:
        ok(f"Windows Firewall allows mDNS discovery (UDP 5353 for {name})")

    if public is True:
        bad("this network is set to PUBLIC in Windows.")
        note("A private-profile rule does nothing on a public network — this is")
        note("an easy hour to lose. Switch it in Settings → Network & Internet")
        note("→ your network → Private.")
        return 1
    if public is False:
        ok("the network is set to Private, so the rule applies")

    if mac == "off":
        ok("the macOS firewall is off, so nothing here blocks the connection")
    elif mac == "block-all":
        bad("the macOS firewall is blocking ALL incoming connections.")
        note("System Settings → Network → Firewall → Options: turn off")
        note("'Block all incoming connections', and allow Python.")
        return 1
    elif mac == "on":
        warn("the macOS firewall is on: make sure it allows Python.")
        firewall_hint(port, admin)
    elif rule is None:
        firewall_hint(port, admin)

    print()
    note("If it still times out, the cause is outside this machine:")
    note("• the phone is on a Guest network, or on 4G rather than Wi-Fi")
    note("• the router has 'AP isolation' / 'client isolation' turned on")
    note("• the 2.4GHz and 5GHz bands are separate networks on your router")
    print()
    note("A quick test from the phone's browser — this should show one short")
    note("line of JSON, and nothing else has to work for it to:")
    note(f"   {url(best or (candidates[0] if candidates else 'ADDRESS'), port, scheme)}/healthz")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
