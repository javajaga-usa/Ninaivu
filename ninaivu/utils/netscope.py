"""Where an address points, for the few places Ninaivu sends a request somewhere
a person typed.

Three features take an address from the console and connect to it: the
creative studio's AI server (photographs go there), the notification webhook
and the off-site bucket. Each had its own idea of which addresses are off
limits, and the ideas disagreed: the webhook refused ``169.254.169.254`` while
the AI server treated it as the home network. The one definition is here.

Three kinds of address:

* **home** — this computer or the home network: loopback, the RFC 1918
  ranges, carrier-grade NAT (which is where Tailscale lives) and IPv6's
  unique local addresses. Plain http is acceptable to these.
* **public** — somewhere on the internet.
* **never** — not a computer anyone would mean: the unspecified address,
  link-local (a cloud machine's metadata service answers at 169.254.169.254;
  a router's set-up pages at fe80::), multicast, and the reserved and
  documentation ranges. No feature has a reason to send anything there.

Only literal addresses and the conventional home-network names are judged;
any other name is ``unknown`` rather than looked up, because a lookup made
while a setting is saved says nothing about where the name resolves later.
Code that is about to connect resolves the name itself and judges what came
back (see :func:`place_ip`).

Standard library only.
"""
from __future__ import annotations

import ipaddress
from typing import Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

#: The ranges a home network uses, and so where a plain http:// address may
#: point. Spelled out rather than read off ``is_private``, which also covers
#: link-local, the documentation ranges and 0.0.0.0/8 — and which has moved
#: between Python versions.
HOME_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10",                        # carrier-grade NAT: Tailscale
    "::1/128", "fc00::/7",                  # IPv6 loopback and unique local
))

#: Suffixes a home gives its machines; ``localhost`` and a bare name
#: (``den-pc``) count too, since both resolve on the house's own network.
HOME_SUFFIXES = (".local", ".lan", ".home.arpa")

#: Bare names that are not a machine in the house but a cloud provider's
#: metadata service, reached by exactly this kind of unqualified lookup.
METADATA_NAMES = frozenset({"metadata", "instance-data"})


def parse_ip(host: str) -> IPAddress | None:
    """*host* as an address when it is a literal one, else None.

    Brackets and an IPv6 zone (``fe80::1%eth0``) are stripped, and an
    IPv4-mapped IPv6 address is judged as the IPv4 address it carries —
    ``::ffff:169.254.169.254`` is the metadata service by another spelling.
    """
    text = (host or "").strip().strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def at_home(ip: IPAddress) -> bool:
    return any(ip.version == net.version and ip in net for net in HOME_NETWORKS)


def never_a_target(ip: IPAddress) -> bool:
    """An address no feature should connect to, whatever the person typed."""
    if at_home(ip):
        return False
    return (ip.is_unspecified or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or not ip.is_global)


def place_ip(ip: IPAddress) -> str:
    """``home``, ``public`` or ``never`` for a literal address."""
    if at_home(ip):
        return "home"
    return "never" if never_a_target(ip) else "public"


def home_name(host: str) -> bool:
    """A name that, by convention, resolves only on the home network."""
    name = (host or "").strip().strip("[]").lower().rstrip(".")
    if not name or name in METADATA_NAMES:
        return False
    return name == "localhost" or name.endswith(".localhost") or "." not in name \
        or name.endswith(HOME_SUFFIXES)


def place(host: str) -> str:
    """``home``, ``public``, ``never`` or ``unknown`` for a host as typed.

    No lookup is made; an ordinary domain name is ``unknown``.
    """
    ip = parse_ip(host)
    if ip is not None:
        return place_ip(ip)
    return "home" if home_name(host) else "unknown"
