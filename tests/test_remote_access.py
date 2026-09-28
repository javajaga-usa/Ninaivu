"""Remote access as a provider, not an assumption (server/remote.py).

Tailscale used to be wired into three places: which addresses are "away from
home", which names the Server page lists, and whose certificate is served.
Each provider answers those three for itself now, and the default is what it
was: Tailscale when it is on the machine, otherwise nothing.
"""

import ipaddress

import pytest

from conftest import ADMIN, login
from ninaivu.server import remote, workload
from ninaivu.server.config import Config


def cfg(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_auto_is_tailscale_only_when_it_is_on_the_machine():
    with_ts = remote.resolve(cfg(), tailscale_present=lambda: True,
                             tailnet_name=lambda: "study.tail1234.ts.net")
    assert with_ts.name == "tailscale" and with_ts.hostnames == ["study.tail1234.ts.net"]
    without = remote.resolve(cfg(), tailscale_present=lambda: False, tailnet_name=lambda: "")
    assert without.name == "none" and without.outside_networks == [] and not without.hostnames


def test_tailscale_counts_its_ranges_as_away_from_home():
    access = remote.resolve(cfg(remote_access="tailscale"), tailnet_name=lambda: "")
    assert access.is_outside(ipaddress.ip_address("100.93.190.76"))
    assert not access.is_outside(ipaddress.ip_address("192.168.1.24"))
    assert access.serves_certificate
    assert access.problems, "no name yet is said, not hidden"


def test_wireguard_needs_its_subnet_and_lists_this_computers_address_on_it():
    bare = remote.resolve(cfg(remote_access="wireguard"), own_addresses=lambda: {"10.8.0.1"})
    assert bare.outside_networks == [] and bare.problems
    access = remote.resolve(cfg(remote_access="wireguard", remote_networks=["10.8.0.0/24"]),
                            own_addresses=lambda: {"192.168.1.24", "10.8.0.1", "127.0.0.1"})
    assert access.is_outside(ipaddress.ip_address("10.8.0.7"))
    assert not access.is_outside(ipaddress.ip_address("192.168.1.40"))
    assert access.addresses == ["10.8.0.1"]
    assert not access.serves_certificate and not access.problems


@pytest.mark.parametrize("name", ["tunnel", "proxy"])
def test_a_tunnel_or_proxy_needs_the_real_address_and_a_name(name):
    access = remote.resolve(cfg(remote_access=name))
    assert len(access.problems) == 2
    ready = remote.resolve(cfg(remote_access=name, trusted_proxies=1,
                               remote_hostname="photos.example.org"))
    assert ready.hostnames == ["photos.example.org"] and not ready.problems
    assert ready.outside_networks == [], "a public address is already away from home"


def test_a_bad_ip_range_is_left_out_not_fatal():
    access = remote.resolve(cfg(remote_access="wireguard", remote_networks=["nonsense", "10.8.0.0/24"]),
                            own_addresses=lambda: set())
    assert [str(n) for n in access.outside_networks] == ["10.8.0.0/24"]


def test_from_outside_uses_the_providers_ranges():
    wg = remote.resolve(cfg(remote_access="wireguard", remote_networks=["10.8.0.0/24"]),
                        own_addresses=lambda: set())
    assert workload.from_outside("10.8.0.7", {}, own=frozenset(), outside_networks=wg.outside_networks)
    assert not workload.from_outside("192.168.1.40", {}, own=frozenset(),
                                     outside_networks=wg.outside_networks)
    # Without a provider, what it always was: Tailscale's ranges.
    assert workload.from_outside("100.93.190.76", {}, own=frozenset())


def test_the_console_saves_the_choice_and_says_what_it_needs(app, people):
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    bad = admin.post("/api/admin/settings", json={"remote_access": "carrier-pigeon"})
    assert bad.status_code == 400
    ok = admin.post("/api/admin/settings", json={"remote_access": "wireguard",
                                                 "remote_networks": "10.8.0.0/24, 10.9.0.0/24"})
    assert ok.status_code == 200, ok.get_json()
    saved = ok.get_json()["settings"]
    assert saved["remote_access"] == "wireguard"
    assert saved["remote_networks"] == ["10.8.0.0/24", "10.9.0.0/24"]
    worse = admin.post("/api/admin/settings", json={"remote_networks": ["10.8.0.0/99"]})
    assert worse.status_code == 400
    page = admin.get("/api/admin/server").get_json()
    access = page.get("addresses", page).get("remote_access") if isinstance(page, dict) else None
    assert access is None or access["name"] == "wireguard"
