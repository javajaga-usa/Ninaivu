"""Remote access: how the household reaches Ninaivu from outside the house.

Ninaivu is served to the home network. Reaching it from elsewhere goes
through something the household set up — Tailscale, a WireGuard tunnel, a
Cloudflare tunnel, a reverse proxy with a public name — and three things in
the server depend on which:

* which addresses count as *away from home* (``server/workload.py``): a
  request from one of them is answered up the house's internet connection,
  which the cloud backup then makes way for;
* which addresses to list on the Server page as *the ones that work away
  from home*;
* whether a certificate for the remote name is handed to browsers asking for
  it, beside Ninaivu's own (Tailscale issues one for its names).

Tailscale used to be assumed for all three. It is one provider here, chosen
by ``Config.remote_access``:

``auto``
    Tailscale when its command is on this machine or it has given this
    machine a name before, otherwise none. The default, so an install that
    never set anything behaves as it did.
``tailscale``
    Addresses in Tailscale's ranges are away from home; the tailnet name is
    listed; its certificate is served.
``wireguard``
    The tunnel's own subnets are away from home — ``Config.remote_networks``,
    because a WireGuard subnet is an ordinary private range that nothing can
    tell from the LAN. The addresses this computer holds inside those subnets
    are listed.
``tunnel``
    A Cloudflare Tunnel or the like: every request arrives from the tunnel's
    connector on this machine with the real address in a forwarded header.
    ``trusted_proxies`` must be at least 1 for the real address to be read
    (then a public address is away from home, as always); the public name in
    ``Config.remote_hostname`` is listed.
``proxy``
    The household's own reverse proxy with a public name: the same as
    ``tunnel``, named for what it is.
``none``
    Nothing is set up. Only a public address is away from home; nothing is
    listed; no extra certificate.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Callable

PROVIDERS = ("auto", "tailscale", "wireguard", "tunnel", "proxy", "none")

#: Tailscale's ranges: the CGNAT block and its IPv6 ULA.
TAILSCALE_NETWORKS = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _networks(values) -> list[Network]:
    out: list[Network] = []
    for value in values or ():
        try:
            out.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            continue
    return out


@dataclass
class RemoteAccess:
    """One way of reaching Ninaivu from outside, resolved from the settings."""
    name: str                       # one of PROVIDERS, never "auto"
    title: str
    #: Address ranges that mean "away from home" on top of the public internet.
    outside_networks: list[Network] = field(default_factory=list)
    #: Host names that work from outside, to list on the Server page.
    hostnames: list[str] = field(default_factory=list)
    #: Addresses this computer holds that work from outside (a tunnel address).
    addresses: list[str] = field(default_factory=list)
    #: Whether a certificate for the remote name is served beside Ninaivu's own.
    serves_certificate: bool = False
    #: What is missing for this provider to work, in words, or nothing.
    problems: list[str] = field(default_factory=list)

    def is_outside(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(ip in net for net in self.outside_networks if net.version == ip.version)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title,
                "outside_networks": [str(n) for n in self.outside_networks],
                "hostnames": list(self.hostnames), "addresses": list(self.addresses),
                "serves_certificate": self.serves_certificate,
                "problems": list(self.problems)}


def chosen(cfg) -> str:
    value = str(getattr(cfg, "remote_access", "auto") or "auto").strip().lower()
    return value if value in PROVIDERS else "auto"


def resolve(cfg, *, tailscale_present: Callable[[], bool] | None = None,
            own_addresses: Callable[[], set[str] | frozenset[str]] | None = None,
            tailnet_name: Callable[[], str] | None = None) -> RemoteAccess:
    """The provider the settings name, with what it needs looked up.

    The three lookups are injectable for the tests; each is answered by the
    real thing when not given.
    """
    name = chosen(cfg)
    saved = tailnet_name if tailnet_name is not None else lambda: _saved_tailnet_name(cfg)
    if name == "auto":
        # Tailscale's command on this machine, or a name it gave this machine
        # before (kept beside the certificates): either says it is in use.
        present = tailscale_present if tailscale_present is not None else _tailscale_present
        name = "tailscale" if (present() or saved()) else "none"

    if name == "tailscale":
        host = saved() or ""
        return RemoteAccess(
            name="tailscale", title="Tailscale",
            outside_networks=_networks(TAILSCALE_NETWORKS),
            hostnames=[host] if host else [],
            serves_certificate=bool(getattr(cfg, "tailnet_https", True)),
            problems=[] if host else ["Tailscale has not given this computer a name yet, "
                                     "or HTTPS certificates are off for the tailnet."])

    if name == "wireguard":
        nets = _networks(getattr(cfg, "remote_networks", None))
        mine = own_addresses if own_addresses is not None else _own_addresses
        inside = []
        for address in sorted(mine()):
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if any(ip in net for net in nets if net.version == ip.version):
                inside.append(address)
        problems = []
        if not nets:
            problems.append("Say which address ranges the tunnel uses (remote_networks), "
                            "so a device on it is known to be away from home.")
        return RemoteAccess(name="wireguard", title="WireGuard", outside_networks=nets,
                            addresses=inside, problems=problems)

    if name in ("tunnel", "proxy"):
        host = str(getattr(cfg, "remote_hostname", "") or "").strip()
        problems = []
        if int(getattr(cfg, "trusted_proxies", 0) or 0) < 1:
            problems.append("Set trusted_proxies to 1 so the real address behind the "
                            f"{'tunnel' if name == 'tunnel' else 'proxy'} is read; until then "
                            "every request through it counts as away from home. It is read "
                            "only from this computer: a proxy on another machine or in "
                            "another container goes in NINAIVU_TRUSTED_PROXY_ADDRESSES.")
        if not host:
            problems.append("Give the public name (remote_hostname) so it can be listed.")
        return RemoteAccess(name=name, title="Cloudflare Tunnel" if name == "tunnel" else "Reverse proxy",
                            hostnames=[host] if host else [], problems=problems)

    return RemoteAccess(name="none", title="None")


# --- the real lookups ------------------------------------------------------

def _tailscale_present() -> bool:
    from ..utils import tailnet                          # noqa: PLC0415
    try:
        return tailnet.find_cli() is not None
    except Exception:                                    # noqa: BLE001
        return False


def _saved_tailnet_name(cfg) -> str:
    from ..utils import tailnet, tls                     # noqa: PLC0415
    try:
        return tailnet.saved_name(tls.tls_dir(cfg.state_dir)) or ""
    except Exception:                                    # noqa: BLE001
        return ""


def _own_addresses():
    from . import workload                               # noqa: PLC0415
    return workload.own_addresses()


#: Headers a public front door adds that Tailscale Serve (tailnet only) does
#: not: Funnel's own mark, and the ones a Cloudflare tunnel or another proxy
#: puts the visitor's address in.
_FUNNEL = "Tailscale-Funnel-Request"
_TAILNET_IDENTITY = ("Tailscale-User-Login", "Tailscale-User-Name")


def _address(value: str | None):
    try:
        ip = ipaddress.ip_address(str(value or "").split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def _on_this_network(ip, own) -> bool:
    """A public IPv6 address in the same /64 as one of this computer's own is
    a device at home: with IPv6 every phone on the Wi-Fi has a global address,
    and the prefix the router hands out is shared by the whole house."""
    if ip.version != 6:
        return False
    home = ipaddress.ip_network(f"{ip}/64", strict=False)
    for address in own:
        mine = _address(address)
        if mine is not None and mine.version == 6 and mine in home:
            return True
    return False


def _public(ip, own) -> bool:
    if ip is None or str(ip) in own:
        return False
    return ip.is_global and not _on_this_network(ip, own)


def from_the_internet(cfg, request, *, own=None) -> bool:
    """Whether *request* came from the internet rather than from home or from a
    device the household let in (Tailscale, WireGuard).

    What opens without a password at home (a tap-to-enter profile, browsing
    without signing in) and the console do not open to it.

    This used to be asked only when ``remote_access`` was "tunnel" or "proxy".
    A router forwarding a port, or a Cloudflare tunnel set up without telling
    Ninaivu, then counted as home, and a tap-to-enter profile opened for
    whoever on the internet found the address. So whatever the setting says:

    * a request from a public address is from the internet, unless it is this
      computer or a device on the house's own IPv6 network;
    * a request through a proxy Ninaivu was not told about (forwarding
      headers with ``trusted_proxies`` at 0) is from the internet, except
      Tailscale Serve, which answers the tailnet only;
    * Tailscale Funnel is the internet.

    Tailscale's and a WireGuard tunnel's addresses are private ranges, so they
    stay devices the household let in.
    """
    from . import auth, workload                                 # noqa: PLC0415
    headers = request.headers
    trusted = int(getattr(cfg, "trusted_proxies", 0) or 0)
    if headers.get(_FUNNEL):
        return True
    if chosen(cfg) in ("tunnel", "proxy") and workload.from_outside(
            request.remote_addr, headers, trusted, outside_networks=[]):
        return True
    own = workload.own_addresses() if own is None else own
    if _public(_address(request.remote_addr), own):
        return True
    if not trusted and any(headers.get(h) for h in auth.FORWARDING_HEADERS
                           if h not in _TAILNET_IDENTITY):
        # Tailscale Serve sets X-Forwarded-For too, and says who on the
        # tailnet is asking; only a connection from this computer can be it.
        peer = _address(request.remote_addr)
        serve = (peer is not None and peer.is_loopback
                 and any(headers.get(h) for h in _TAILNET_IDENTITY))
        return not serve
    return False


def plain_http_from_internet(request, *, own=None) -> bool:
    """A connection straight from the internet, without HTTPS: a router
    forwarding a port to Ninaivu's plain-HTTP listener.

    Every password, PIN, session cookie and share link would cross the
    internet readable by anyone on the way, so nothing is answered. The peer
    is the connection's own address, before any trusted proxy rewrote it: a
    proxy on this computer or the house's network is the household's own
    front door, and says itself whether the browser used HTTPS.
    """
    from . import workload                                       # noqa: PLC0415
    if request.is_secure:
        return False
    orig = request.environ.get("werkzeug.proxy_fix.orig") or {}
    peer = orig.get("REMOTE_ADDR") or request.remote_addr
    own = workload.own_addresses() if own is None else own
    return _public(_address(peer), own)
