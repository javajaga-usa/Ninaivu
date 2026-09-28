#!/usr/bin/env python3
"""Prove that `ninaivu.local` really resolves and serves, from off-box.

Run against a started Ninaivu that was given `--host 0.0.0.0`:

    python tests/mdns_check.py [name] [port]

It asks the network for the name exactly as another device's resolver would —
a multicast DNS query for an A record — then fetches the health endpoint at
whatever address comes back. Both halves have to pass for the name to be
usable from a phone.
"""

from __future__ import annotations

import socket
import struct
import sys
import time
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

NAME = sys.argv[1] if len(sys.argv) > 1 else "ninaivu"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5000


def resolve(hostname: str, timeout: float = 6.0) -> list[str]:
    """Multicast an A query for *hostname* and collect the answers.

    Uses python-zeroconf's own packet parser rather than a hand-rolled one:
    an earlier version of this script parsed the response by hand, mishandled
    DNS name compression, and reported a working setup as broken.
    """
    from zeroconf import DNSIncoming, DNSOutgoing, DNSQuestion
    from zeroconf import const as C

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 5353))
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
        struct.pack("4sl", socket.inet_aton("224.0.0.251"), socket.INADDR_ANY))
    sock.settimeout(1.0)

    question = DNSOutgoing(C._FLAGS_QR_QUERY)
    question.add_question(DNSQuestion(f"{hostname}.", C._TYPE_A, C._CLASS_IN))
    sock.sendto(question.packets()[0], ("224.0.0.251", 5353))

    addresses: list[str] = []
    deadline = time.time() + timeout
    while time.time() < deadline and not addresses:
        try:
            data, _ = sock.recvfrom(9000)
        except socket.timeout:
            continue
        for record in DNSIncoming(data).answers():
            if getattr(record, "name", "").rstrip(".") != hostname:
                continue
            address = getattr(record, "address", None)
            if address and len(address) == 4:
                addresses.append(socket.inet_ntoa(address))
    sock.close()
    return sorted(set(addresses))


def main() -> int:
    hostname = f"{NAME}.local"
    failures = 0

    print(f"\n  Looking for {hostname} on the network…\n")
    try:
        addresses = resolve(hostname)
    except ImportError:
        print("  SKIP  zeroconf is not installed here")
        return 0

    if addresses:
        print(f"  PASS  {hostname} resolves → {', '.join(addresses)}")
    else:
        print(f"  FAIL  nothing answered for {hostname}")
        print("        Is Ninaivu running with --host 0.0.0.0, and without")
        print("        --no-mdns? Is zeroconf installed?")
        return 1

    for address in addresses:
        url = f"http://{address}:{PORT}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read(120).decode("utf-8", "replace")
            if response.status == 200 and '"ok"' in body.replace(" ", ""):
                print(f"  PASS  {url} → 200 {body.strip()}")
            else:
                print(f"  FAIL  {url} → {response.status} {body.strip()}")
                failures += 1
        except Exception as exc:                 # noqa: BLE001
            print(f"  FAIL  {url} → {exc}")
            failures += 1

    print()
    if failures:
        return 1
    print(f"  A phone on this network can open http://{hostname}:{PORT}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
