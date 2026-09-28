"""Picking the address a phone can actually reach.

A Windows machine with Hyper-V, WSL, VirtualBox or a VPN has several IPv4
addresses and several of them start 192.168., so "looks like a LAN address" is
not enough. The adapter with a default gateway is the one on the home network;
the virtual ones have none. Getting this wrong is what makes a phone sit and
time out rather than fail fast, so it is worth pinning down properly.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import netinfo


# Real `ipconfig` output from a Windows 11 machine running Hyper-V, WSL2 and
# Tailscale — the exact shape that defeats a naive "starts with 192.168." test.
IPCONFIG = """
Windows IP Configuration


Ethernet adapter vEthernet (Default Switch):

   Connection-specific DNS Suffix  . :
   Link-local IPv6 Address . . . . . : fe80::a1b2:c3d4:e5f6:1234%21
   IPv4 Address. . . . . . . . . . . : 192.168.176.1
   Subnet Mask . . . . . . . . . . . : 255.255.240.0
   Default Gateway . . . . . . . . . :

Ethernet adapter vEthernet (WSL (Hyper-V firewall)):

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 172.29.16.1
   Subnet Mask . . . . . . . . . . . : 255.255.240.0
   Default Gateway . . . . . . . . . :

Unknown adapter Tailscale:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 100.101.102.103
   Subnet Mask . . . . . . . . . . . : 255.255.255.255
   Default Gateway . . . . . . . . . :

Wireless LAN adapter Wi-Fi:

   Connection-specific DNS Suffix  . : home
   IPv6 Address. . . . . . . . . . . : 2001:db8::1
   IPv4 Address. . . . . . . . . . . : 192.168.1.24
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.1.1

Ethernet adapter Bluetooth Network Connection:

   Media State . . . . . . . . . . . : Media disconnected
   Connection-specific DNS Suffix  . :
"""


@pytest.fixture()
def windows(monkeypatch):
    monkeypatch.setattr(netinfo.sys, "platform", "win32")
    monkeypatch.setattr(netinfo, "_run", lambda command: IPCONFIG)


def test_every_adapter_with_an_address_is_listed(windows):
    names = [a["name"] for a in netinfo.adapters()]
    assert "Wireless LAN adapter Wi-Fi" in names
    assert "Ethernet adapter vEthernet (Default Switch)" in names
    # The disconnected one has no address, so it is not a candidate at all.
    assert not any("Bluetooth" in n for n in names)


def test_only_the_real_network_has_a_gateway(windows):
    assert netinfo.gateway_addresses() == ["192.168.1.24"]


def test_the_chosen_address_is_the_wifi_one_not_the_hyperv_one(windows):
    assert netinfo.best_address() == "192.168.1.24", (
        "Hyper-V's 192.168.176.1 looks just as much like a home address, and a "
        "phone told to use it waits until it times out")


def test_tailscale_and_wsl_are_not_offered(windows):
    chosen = netinfo.best_address()
    assert not chosen.startswith("100.")
    assert not chosen.startswith("172.")


def test_no_gateway_anywhere_returns_nothing_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(netinfo.sys, "platform", "win32")
    monkeypatch.setattr(netinfo, "_run", lambda command: """
Ethernet adapter vEthernet (Default Switch):

   IPv4 Address. . . . . . . . . . . : 192.168.176.1
   Default Gateway . . . . . . . . . :
""")
    assert netinfo.best_address() is None, (
        "offering an address with no route out is worse than saying nothing")


def test_unparsable_output_is_soft(monkeypatch):
    monkeypatch.setattr(netinfo.sys, "platform", "win32")
    monkeypatch.setattr(netinfo, "_run", lambda command: "")
    assert netinfo.adapters() == []
    assert netinfo.best_address() is None


def test_linux_falls_back_to_proc_when_there_is_no_ip_binary(monkeypatch):
    """Containers and cut-down systems have /proc/net/route and no `ip`."""
    if not Path("/proc/net/route").is_file():
        # The fallback reads the host's real routing table; only a Linux
        # machine has one to read.
        pytest.skip("needs a Linux /proc/net/route to read")
    monkeypatch.setattr(netinfo.sys, "platform", "linux")
    monkeypatch.setattr(netinfo, "_run", lambda command: "")
    rows = netinfo.adapters()
    assert rows, "the routing table was readable and still produced nothing"
    assert all(r["gateway"] for r in rows)


def test_same_network_compares_the_subnet():
    assert netinfo.same_network("192.168.1.24", "192.168.1.1")
    assert not netinfo.same_network("192.168.1.24", "192.168.176.1")


# --- the startup warning ----------------------------------------------------

def test_no_firewall_warning_off_windows(monkeypatch):
    from ninaivu import __main__ as entry
    monkeypatch.setattr(entry.sys, "platform", "linux")
    assert entry._firewall_warning(80) == []


def test_the_banner_warns_when_the_rule_is_missing(monkeypatch):
    """A missing rule is the usual reason another device 'loads forever'."""
    from ninaivu import __main__ as entry

    class Result:
        stdout = "No rules match the specified criteria."

    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(entry.subprocess, "run", lambda *a, **k: Result())
    lines = entry._firewall_warning(80)
    assert lines and "allow-network.bat" in "\n".join(lines)


def _netsh(tcp_ports, mdns=True):
    """A stand-in for `netsh ... show rule name=<rule>`, answering per rule.

    Ninaivu asks about two rules — the TCP ports and, separately, mDNS on UDP
    5353 — so a fake that returns one fixed text for every call describes a
    machine that cannot exist, and always looks like mDNS is missing.
    """
    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def run(args, *_, **__):
        if "name=Ninaivu mDNS" in args:
            return Result("Rule Name: Ninaivu mDNS\nLocalPort: 5353\nProtocol: UDP\nEnabled: Yes"
                          if mdns else "No rules match the specified criteria.")
        return Result(f"Rule Name: Ninaivu\nLocalPort: {tcp_ports}\nEnabled: Yes")
    return run


def test_the_banner_stays_quiet_when_the_rule_is_there(monkeypatch):
    from ninaivu import __main__ as entry

    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(entry.subprocess, "run", _netsh("80,5000,3000"))
    assert entry._firewall_warning(80) == []


def test_the_banner_warns_when_only_mdns_is_missing(monkeypatch):
    from ninaivu import __main__ as entry

    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(entry.subprocess, "run", _netsh("80,5000,3000", mdns=False))
    assert "5353" in "\n".join(entry._firewall_warning(80))


def test_the_banner_checks_the_port_actually_in_use_not_a_fixed_one(monkeypatch):
    """A rule from before the family app's default port changed to 80 does
    not cover 80 — the check has to compare against the port Ninaivu is
    actually bound to, not a number baked into the function."""
    from ninaivu import __main__ as entry

    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(entry.subprocess, "run", _netsh("5000,3000"))
    assert entry._firewall_warning(5000) == []
    assert entry._firewall_warning(80) != []


def test_a_broken_netsh_is_not_fatal(monkeypatch):
    from ninaivu import __main__ as entry

    def boom(*a, **k):
        raise OSError("netsh is not on PATH")

    monkeypatch.setattr(entry.sys, "platform", "win32")
    monkeypatch.setattr(entry.subprocess, "run", boom)
    assert entry._firewall_warning(80) == []


# --- the address the banner offers ------------------------------------------

def test_lan_addresses_puts_the_routed_adapter_first(monkeypatch):
    """Hyper-V's 192.168.176.1 must not outrank the real Wi-Fi address."""
    from ninaivu.utils import netinfo as bundled
    from ninaivu.utils import tls

    monkeypatch.setattr(tls, "_probe", lambda target: None)
    monkeypatch.setattr(bundled, "gateway_addresses", lambda: ["192.168.1.24"])
    monkeypatch.setattr(
        tls.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", (addr, 0)) for addr in
                         ("192.168.0.1", "192.168.1.24", "172.29.16.1")])
    assert tls.lan_addresses()[0] == "192.168.1.24"
