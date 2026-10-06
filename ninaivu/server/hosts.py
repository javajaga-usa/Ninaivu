"""Which names Ninaivu answers to — the defence against DNS rebinding.

A page on some website can point its own domain at this computer's home
address (DNS rebinding). The browser then treats that page and Ninaivu as the
same origin: the cross-origin check passes, the page can sign in to a
tap-to-enter profile and read the library. What gives it away is the Host
header — it carries the attacker's domain, because that is the name the
browser looked up.

So a request is answered only when its Host is a name a household can
actually be using for its own server:

* an IP address, written as one — a rebinding page cannot make the browser
  send a bare address as its Host;
* ``localhost`` and names under ``.localhost``;
* a name with no dot at all (``ninaivu``, ``nas``: the router's own DNS);
* names under the suffixes only a home network hands out — ``.local``
  (mDNS), ``.lan``, ``.home``, ``.home.arpa``, ``.internal``,
  ``.localdomain`` — and ``.ts.net`` (Tailscale; nobody else can register
  under it);
* this computer's own host name;
* ``Config.remote_hostname`` and anything in ``Config.allowed_hosts`` — a
  public name the household set up for a tunnel or a reverse proxy.

Anything else is refused with 421, saying which setting lets it in. Behind a
reverse proxy, the name the proxy forwards (``X-Forwarded-Host``) is checked
instead — and only when ``trusted_proxies`` says a proxy is there, since
otherwise any client could write that header.

"A proxy is there" is also only believed of a connection that comes from
where a proxy is: this computer, or an address listed in
``NINAIVU_TRUSTED_PROXY_ADDRESSES`` (:class:`ProxyFromTrustedPeers`). With
``trusted_proxies`` at 1, a phone on the home network that wrote
``X-Forwarded-For: 127.0.0.1`` used to be taken for this computer.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from functools import lru_cache
from typing import Any, Iterable

from werkzeug.middleware.proxy_fix import ProxyFix

log = logging.getLogger(__name__)

#: Suffixes only a home network, or Tailscale, hands out.
HOME_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain",
                 ".localhost", ".ts.net")


def _name(host: str) -> str:
    """The host part of a Host header, lower-cased, without port or brackets."""
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("["):                        # [::1]:8080
        return host[1:host.find("]")] if "]" in host else host[1:]
    if host.count(":") == 1:                       # name:port or v4:port
        host = host.split(":", 1)[0]
    return host


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _own_names() -> frozenset[str]:
    names = set()
    try:
        own = socket.gethostname().lower().rstrip(".")
        names.update({own, own.split(".", 1)[0]})
        names.add(socket.getfqdn().lower().rstrip("."))
    except OSError:
        pass
    return frozenset(n for n in names if n)


def configured(cfg: Any) -> set[str]:
    """The public names the household told Ninaivu about."""
    out = set()
    remote = str(getattr(cfg, "remote_hostname", "") or "").strip().lower().rstrip(".")
    if remote:
        out.add(_name(remote))
    for host in getattr(cfg, "allowed_hosts", None) or []:
        host = _name(str(host))
        if host:
            out.add(host)
    return out


def allowed(host: str, cfg: Any = None, extra: Iterable[str] = ()) -> bool:
    """May a request whose Host is *host* be answered?"""
    name = _name(host)
    if not name:
        return True                                # HTTP/1.0 with no Host: not a browser
    if _is_ip(name) or name == "localhost" or "." not in name:
        return True
    if name.endswith(HOME_SUFFIXES):
        return True
    if name in _own_names():
        return True
    wanted = configured(cfg) | {_name(e) for e in extra}
    for entry in wanted:
        if entry.startswith("*.") and name.endswith(entry[1:]):
            return True
        if name == entry:
            return True
    return False


def checked_host(headers: Any, host: str, cfg: Any) -> str:
    """The name to check: the proxy's forwarded one when a proxy is trusted."""
    if int(getattr(cfg, "trusted_proxies", 0) or 0) > 0:
        forwarded = (headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return host


#: The headers a proxy writes for the application to read, as WSGI keys.
#: Removed from a request that did not come through a trusted proxy, so that
#: nothing further in can read them by mistake.
_PROXY_KEYS = ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PROTO",
               "HTTP_X_FORWARDED_PORT", "HTTP_X_FORWARDED_PREFIX", "HTTP_FORWARDED",
               "HTTP_X_REAL_IP")


def trusted_proxy_networks(raw: str | None = None) -> tuple[Any, ...]:
    """The addresses, besides this computer's loopback, a proxy may connect
    from: ``NINAIVU_TRUSTED_PROXY_ADDRESSES``, addresses or ranges separated
    by commas (``172.18.0.0/16`` for a proxy in another Docker container)."""
    if raw is None:
        raw = os.environ.get("NINAIVU_TRUSTED_PROXY_ADDRESSES", "")
    out = []
    for item in str(raw or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("NINAIVU_TRUSTED_PROXY_ADDRESSES: %r is not an address or a "
                        "range; left out", item)
    return tuple(out)


def peer_is_trusted_proxy(address: str | None, networks: Iterable[Any] = ()) -> bool:
    """Whether a connection from *address* may be a proxy Ninaivu believes."""
    try:
        ip = ipaddress.ip_address(str(address or "").split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    return any(ip in net for net in networks if net.version == ip.version)


class ProxyFromTrustedPeers(ProxyFix):
    """ProxyFix for the connections that come from a proxy, and nobody else.

    The peer is the address the connection itself came from, which the
    caller cannot write. From this computer (where Caddy and Tailscale Serve
    run) or a listed address, the forwarding headers are read as ProxyFix
    reads them. From anywhere else they are removed before Ninaivu sees them:
    otherwise any device on the network could name itself 127.0.0.1, or a
    host name of its choosing, and be believed.
    """

    def __init__(self, app: Any, *, networks: Iterable[Any] = (), **kwargs: Any):
        super().__init__(app, **kwargs)
        self.networks = tuple(networks)
        self._warned = False

    def __call__(self, environ: Any, start_response: Any) -> Any:
        peer = environ.get("REMOTE_ADDR")
        if peer_is_trusted_proxy(peer, self.networks):
            return super().__call__(environ, start_response)
        dropped = [key for key in _PROXY_KEYS if environ.pop(key, None) is not None]
        if dropped and not self._warned:
            self._warned = True
            log.warning("A request from %s carried proxy headers, which were ignored: "
                        "trusted_proxies believes them only from this computer. If a "
                        "proxy on another machine or container connects from there, "
                        "list it in NINAIVU_TRUSTED_PROXY_ADDRESSES.", peer)
        return self.app(environ, start_response)
