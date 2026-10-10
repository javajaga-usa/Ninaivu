"""Home and the internet, judged in a container and across IPv6 (10 October
2026 audit).

Two places where ``remote.from_the_internet`` said "home" to the internet:
inside a Docker container, where every published port is forwarded and the
only address that arrives is Docker's own gateway; and on a machine whose
global IPv6 /64 is shared with strangers (a VPS, a tunnel broker), where any
address in the same /64 counted as a phone on the Wi-Fi.
"""

from __future__ import annotations

import sys
import types

import pytest

from conftest import ADMIN

PUBLIC = "93.184.216.34"
GATEWAY = "172.17.0.1"           # Docker's bridge on Linux (the userland proxy)
DESKTOP = "192.168.65.1"         # Docker Desktop, Mac and Windows
LAN = "192.168.1.20"

#: What a container holds: loopback and its own address on the bridge.
CONTAINER_OWN = frozenset({"127.0.0.1", "::1", "172.17.0.2"})


class _Request:
    def __init__(self, addr, headers=None, secure=False, orig=None, port=None):
        self.remote_addr = addr
        self.headers = headers or {}
        self.is_secure = secure
        self.environ = {}
        if orig:
            self.environ["werkzeug.proxy_fix.orig"] = orig
        if port is not None:
            self.environ["SERVER_PORT"] = str(port)


class _Cfg:
    remote_access = "auto"
    trusted_proxies = 0
    remote_networks: list = []
    port = 5000
    admin_port = 3000


def judged(request, *, cfg=None, container=True, own=CONTAINER_OWN, interfaces=None):
    from ninaivu.server import remote
    return remote.from_the_internet(cfg or _Cfg(), request, own=own,
                                    interfaces=interfaces, container=container)


# -- (a) in a container, without a proxy ------------------------------------------

@pytest.mark.parametrize("addr, expected", [
    (GATEWAY, True),                     # whoever Docker forwarded, Linux
    (DESKTOP, True),                     # whoever Docker forwarded, Desktop
    (LAN, True),                         # a real LAN address, or a forwarder's
    ("10.0.0.8", True),
    ("fd12:3456::9", True),              # a ULA: private, and not a tunnel's
    (PUBLIC, True),                      # a public address was always the internet
    ("127.0.0.1", False),                # this container (the health check)
    ("172.17.0.2", False),               # its own bridge address
    ("100.101.102.103", False),          # Tailscale, in the container's own netns
    ("fd7a:115c:a1e0::1", False),        # Tailscale's IPv6
])
def test_in_a_container_only_this_computer_and_a_tunnel_are_home(addr, expected):
    assert judged(_Request(addr)) is expected


def test_a_wireguard_subnet_the_household_named_is_still_a_tunnel_in_a_container():
    class Cfg(_Cfg):
        remote_networks = ["10.8.0.0/24"]
    assert judged(_Request("10.8.0.2"), cfg=Cfg()) is False
    assert judged(_Request("10.9.0.2"), cfg=Cfg()) is True


# -- (b) in a container, behind a proxy Ninaivu trusts -------------------------------

def test_behind_a_trusted_proxy_the_forwarded_address_is_judged_as_usual():
    class Cfg(_Cfg):
        trusted_proxies = 1
    # ProxyFix has put the real address in remote_addr and the proxy's in orig.
    assert judged(_Request(LAN, orig={"REMOTE_ADDR": GATEWAY}), cfg=Cfg()) is False
    assert judged(_Request(PUBLIC, orig={"REMOTE_ADDR": GATEWAY}), cfg=Cfg()) is True


def test_trusted_proxies_alone_does_not_make_a_direct_connection_home():
    """The household set trusted_proxies for its proxy, and also forwarded a
    router port straight to the container, past it: nothing named the real
    address, so the forwarder's own is still nobody known."""
    class Cfg(_Cfg):
        trusted_proxies = 1
    assert judged(_Request(GATEWAY), cfg=Cfg()) is True
    # ProxyFix ran (the peer was a trusted proxy) but no address was forwarded.
    assert judged(_Request(GATEWAY, orig={"REMOTE_ADDR": GATEWAY}), cfg=Cfg()) is True


# -- (c) outside a container, nothing changes -----------------------------------------

@pytest.mark.parametrize("addr, expected", [
    (GATEWAY, False), (DESKTOP, False), (LAN, False), ("10.0.0.8", False),
    ("127.0.0.1", False), (PUBLIC, True),
])
def test_outside_a_container_a_private_peer_is_home(addr, expected):
    own = frozenset({"127.0.0.1", "::1", "192.168.1.5"})
    assert judged(_Request(addr), container=False, own=own) is expected


# -- the console in a container ----------------------------------------------------------

def test_the_console_listener_in_a_container_still_opens_to_the_host():
    """The shipped compose file publishes the console on the host's loopback
    alone; what reaches it is the host, through the same forwarder. Judged the
    internet, the console would refuse the household that runs it."""
    assert judged(_Request(GATEWAY, port=3000)) is False
    assert judged(_Request(GATEWAY, port=5000)) is True
    # A public address at the console's port is still the internet.
    assert judged(_Request(PUBLIC, port=3000)) is True


def test_the_container_is_recognised_by_the_run_file_module(monkeypatch):
    from ninaivu.server import remote
    monkeypatch.setattr(remote, "_in_container", lambda: True)
    assert remote.from_the_internet(_Cfg(), _Request(GATEWAY), own=CONTAINER_OWN) is True
    monkeypatch.setattr(remote, "_in_container", lambda: False)
    assert remote.from_the_internet(_Cfg(), _Request(GATEWAY), own=CONTAINER_OWN) is False


def _as_a_container(monkeypatch):
    """Inside a container, with a container's own addresses. On a computer
    running Docker (GitHub's Linux runners do) the bridge's 172.17.0.1 is this
    computer's own address, and a request from it never left the computer."""
    from ninaivu.server import remote, workload
    monkeypatch.setattr(remote, "_in_container", lambda: True)
    monkeypatch.setattr(workload, "own_addresses", lambda: CONTAINER_OWN)
    monkeypatch.setattr(workload, "own_interfaces", lambda: (CONTAINER_OWN,))


def test_in_a_container_the_family_app_asks_the_lan_to_sign_in(app, cfg, people, monkeypatch):
    _as_a_container(monkeypatch)
    cfg.open_browsing = True
    browser = app.test_client()
    assert browser.get("/api/assets", environ_base={"REMOTE_ADDR": GATEWAY}).status_code == 401
    assert browser.get("/api/assets", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200


def test_in_a_container_the_console_still_signs_the_household_in(scanned, people, monkeypatch):
    from ninaivu import build_services, create_admin_app
    _as_a_container(monkeypatch)
    cfg, _, _ = scanned
    cfg.port, cfg.admin_port = 5000, 3000
    services = build_services(cfg)
    services.scanner.stop()
    console = create_admin_app(services).test_client()
    body = {"username": ADMIN[0], "password": ADMIN[1]}
    at_its_port = console.post("/api/auth/login", json=body, base_url="http://localhost:3000",
                               environ_base={"REMOTE_ADDR": GATEWAY})
    assert at_its_port.status_code == 200
    elsewhere = console.post("/api/auth/login", json=body, base_url="http://localhost:5000",
                             environ_base={"REMOTE_ADDR": GATEWAY})
    assert elsewhere.status_code == 403
    assert "console_from_internet" in elsewhere.get_json()["error"]


# -- (d) the IPv6 /64 ------------------------------------------------------------------

V6_HOME = "2a02:1234:5678:9abc::5"       # this computer's global address
PHONE = "2a02:1234:5678:9abc::77"        # the same /64
LOOPBACK = frozenset({"127.0.0.1", "::1"})


def _interfaces(*groups):
    return (LOOPBACK, *[frozenset(g) for g in groups])


def test_the_same_64_is_home_on_the_house_wifi():
    wifi = _interfaces({"192.168.1.5", V6_HOME, "fe80::1"})
    assert judged(_Request(PHONE), container=False, own=LOOPBACK, interfaces=wifi) is False


def test_the_same_64_is_not_home_on_a_bare_uplink():
    """A VPS: one public IPv4, one global IPv6 out of a /64 the provider
    shares, and the link-local every interface has. (Not a documentation
    address for the IPv4: ipaddress calls 203.0.113.0/24 private.)"""
    uplink = _interfaces({"198.41.0.4", V6_HOME, "fe80::1"})
    assert judged(_Request(PHONE), container=False, own=LOOPBACK, interfaces=uplink) is True


def test_the_64_of_a_tunnel_interface_is_not_home_even_beside_a_lan_one():
    """A tunnel broker's or a VPN's interface shares its /64 with strangers;
    the LAN interface next to it does not lend it the house."""
    both = _interfaces({"192.168.1.5"}, {V6_HOME, "fe80::1"})
    assert judged(_Request(PHONE), container=False, own=LOOPBACK, interfaces=both) is True


def test_a_ula_beside_the_global_address_marks_the_house_too():
    v6_only_house = _interfaces({"fd12:3456::1", V6_HOME, "fe80::1"})
    assert judged(_Request(PHONE), container=False, own=LOOPBACK,
                  interfaces=v6_only_house) is False


def test_without_the_interfaces_a_private_address_anywhere_is_the_most_that_can_be_asked():
    bare = frozenset({*LOOPBACK, V6_HOME})
    assert judged(_Request(PHONE), container=False, own=bare) is True
    house = frozenset({*bare, "192.168.1.5"})
    assert judged(_Request(PHONE), container=False, own=house) is False


def test_plain_http_judges_the_64_the_same_way():
    from ninaivu.server import remote
    uplink = _interfaces({"198.41.0.4", V6_HOME, "fe80::1"})
    wifi = _interfaces({"192.168.1.5", V6_HOME, "fe80::1"})
    assert remote.plain_http_from_internet(_Request(PHONE), own=LOOPBACK, interfaces=uplink)
    assert not remote.plain_http_from_internet(_Request(PHONE), own=LOOPBACK, interfaces=wifi)


def test_own_interfaces_keeps_the_grouping_and_own_addresses_flattens_it(monkeypatch):
    import socket
    from ninaivu.server import workload

    snic = types.SimpleNamespace
    fake = types.SimpleNamespace(net_if_addrs=lambda: {
        "lo": [snic(family=socket.AF_INET, address="127.0.0.1")],
        "eth0": [snic(family=socket.AF_INET, address="198.41.0.4"),
                 snic(family=socket.AF_INET6, address=f"{V6_HOME}%eth0"),
                 snic(family=-1, address="00:11:22:33:44:55")],
    })
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(workload, "_own", (0.0, (), frozenset()))
    groups = workload.own_interfaces()
    assert frozenset({"198.41.0.4", V6_HOME}) in groups
    assert workload.own_addresses() == frozenset({"127.0.0.1", "::1", "198.41.0.4", V6_HOME})
    monkeypatch.setattr(workload, "_own", (0.0, (), frozenset()))


# -- what the settings and the notes say -----------------------------------------------

def test_trusted_proxies_says_what_the_number_means():
    from ninaivu.server import settings_groups
    doc = settings_groups._docs()["trusted_proxies"]
    assert "never higher" in doc and "number of proxies" in doc
    assert settings_groups.RANGES["trusted_proxies"] == (0, 10)


def test_the_security_notes_say_what_a_container_hides_and_what_the_backup_holds():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    assert "Docker Desktop" in text and "NINAIVU_TRUSTED_PROXY_ADDRESSES" in text
    assert "holds session tokens" not in text
    assert "SMTP password" in text and "TLS CA key" in text
