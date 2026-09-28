#!/usr/bin/env python3
"""Which of this machine's addresses is actually on the home network?

A Windows PC with Hyper-V, WSL, VirtualBox, Docker or a VPN installed has
several IPv4 addresses, and several of them start ``192.168.`` — so "looks like
a LAN address" is not enough to pick the right one. A phone that tries the
wrong one gets no reply at all and sits there until it times out, which is the
single most confusing way for this to fail: nothing is refused, nothing errors,
it just never loads.

The reliable signal is the **default gateway**. The adapter that has one is the
adapter that reaches the rest of the world, and therefore the one the phone is
on. Virtual adapters almost never have a gateway.

Standard library only, and every failure is soft: if the OS output cannot be
parsed this returns nothing rather than guessing, and the caller falls back to
the ordinary ranking.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys

__all__ = ["gateway_addresses", "adapters", "best_address"]

_IPV4 = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout or ""


def _windows_adapters() -> list[dict[str, str]]:
    """Parse ``ipconfig`` into ``{name, address, gateway}`` per adapter."""
    text = _run(["ipconfig"])
    if not text:
        return []
    found: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith(" "):
            if current.get("address"):
                found.append(current)
            current = {"name": line.strip().rstrip(":"), "address": "", "gateway": ""}
            continue
        low = line.lower()
        match = _IPV4.search(line)
        if not match:
            continue
        if "ipv4 address" in low and not current.get("address"):
            current["address"] = match.group(1)
        elif "default gateway" in low and not current.get("gateway"):
            current["gateway"] = match.group(1)
    if current.get("address"):
        found.append(current)
    return found


def _proc_gateways() -> dict[str, str]:
    """Default gateways from /proc/net/route, for systems with no `ip` binary."""
    gateway_for: dict[str, str] = {}
    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            next(handle, None)
            for line in handle:
                fields = line.split()
                if len(fields) < 3 or fields[1] != "00000000":
                    continue
                packed = int(fields[2], 16)
                gateway_for[fields[0]] = ".".join(
                    str((packed >> shift) & 0xFF) for shift in (0, 8, 16, 24))
    except (OSError, ValueError):
        pass
    return gateway_for


def _posix_adapters() -> list[dict[str, str]]:
    routes = _run(["ip", "route"])
    gateway_for: dict[str, str] = {}
    for line in routes.splitlines():
        if line.startswith("default via "):
            parts = line.split()
            try:
                gateway_for[parts[parts.index("dev") + 1]] = parts[2]
            except (ValueError, IndexError):
                continue
    if not gateway_for:
        gateway_for = _proc_gateways()
    found = []
    for line in _run(["ip", "-4", "-o", "addr"]).splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        name, address = parts[1], parts[3].split("/")[0]
        if address.startswith("127."):
            continue
        found.append({"name": name, "address": address,
                      "gateway": gateway_for.get(name, "")})
    if not found and gateway_for:
        # No `ip` binary. Ask the routing table which of our addresses reaches
        # the gateway, which is the same question by another route.
        for interface, gateway in gateway_for.items():
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect((gateway, 9))
                found.append({"name": interface,
                              "address": probe.getsockname()[0],
                              "gateway": gateway})
            except OSError:
                continue
            finally:
                probe.close()
    return found


def _darwin_adapters() -> list[dict[str, str]]:
    """macOS has neither `ip` nor /proc, so ask route and ifconfig instead.

    `route -n get default` names the interface that reaches the world, which is
    the same question the Windows and Linux branches ask in their own dialects.
    """
    gateway = interface = ""
    for line in _run(["route", "-n", "get", "default"]).splitlines():
        cleaned = line.strip()
        if cleaned.startswith("gateway:"):
            gateway = cleaned.split(":", 1)[1].strip()
        elif cleaned.startswith("interface:"):
            interface = cleaned.split(":", 1)[1].strip()

    found: list[dict[str, str]] = []
    current = ""
    for line in _run(["ifconfig"]).splitlines():
        if line and not line[0].isspace():
            current = line.split(":", 1)[0]
            continue
        cleaned = line.strip()
        if not cleaned.startswith("inet "):
            continue
        address = cleaned.split()[1]
        if address.startswith("127."):
            continue
        found.append({"name": current, "address": address,
                      "gateway": gateway if current == interface else ""})
    return found


def adapters() -> list[dict[str, str]]:
    """Every IPv4 adapter, with the default gateway it has (or "")."""
    try:
        if sys.platform == "win32":
            return _windows_adapters()
        if sys.platform == "darwin":
            return _darwin_adapters()
        return _posix_adapters()
    except Exception:                            # noqa: BLE001 — diagnostics only
        return []


def gateway_addresses() -> list[str]:
    """Our addresses on adapters that actually have a default gateway."""
    return [a["address"] for a in adapters() if a["gateway"] and a["address"]]


def best_address() -> str | None:
    """The one address most likely to work from a phone, or None if unsure."""
    routed = gateway_addresses()
    if len(routed) == 1:
        return routed[0]
    for address in routed:                        # prefer an ordinary home range
        if address.startswith("192.168."):
            return address
    return routed[0] if routed else None


def same_network(a: str, b: str) -> bool:
    """A /24 comparison — good enough for every home router ever shipped."""
    return a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


if __name__ == "__main__":
    rows = adapters()
    if not rows:
        print("  Could not read this machine's adapters.")
        raise SystemExit(1)
    width = max(len(r["name"]) for r in rows)
    print()
    for row in rows:
        mark = "← the home network" if row["gateway"] else "  (no gateway — virtual/inactive)"
        print(f"  {row['name']:<{width}}  {row['address']:<15} {mark}")
    print()
    best = best_address()
    print(f"  Use this one on phones and tablets: {best}" if best
          else "  No adapter has a default gateway — is this machine online?")
    print()
