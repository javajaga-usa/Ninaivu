"""tools/netcheck.py: it asks the running Ninaivu where it is, and asks the
network for its name the way a phone would.

It used to assume port 5000. On a Mac that is AirPlay Receiver, so it reported
"the family app is answering on port 5000" and told phones to use an address
that was not Ninaivu at all — and it never checked ninaivu.local.
"""
import importlib.util
import json
import socket
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("netcheck", ROOT / "tools" / "netcheck.py")
netcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(netcheck)


def test_no_port_is_assumed():
    source = (ROOT / "tools" / "netcheck.py").read_text(encoding="utf-8")
    assert "FAMILY_PORT" not in source and ":5000" not in source


def test_the_port_comes_from_the_running_ninaivu(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    (tmp_path / "ninaivu.run").write_text(json.dumps(
        {"port": 443, "admin_port": 3000, "scheme": "https", "host": "0.0.0.0"}))
    assert netcheck.running()["port"] == 443
    (tmp_path / "ninaivu.run").unlink()
    assert netcheck.running() is None


def test_an_address_leaves_out_its_schemes_own_port():
    assert netcheck.url("ninaivu.local", 443, "https") == "https://ninaivu.local"
    assert netcheck.url("192.168.0.229", 64934, "https") == "https://192.168.0.229:64934"
    assert netcheck.url("ninaivu.local", 80, "http") == "http://ninaivu.local"


def _reply(name, address, *, question=True, other=None):
    def encode(n):
        return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"
    body, answers = b"", 1 + (1 if other else 0)
    if question:
        body += encode(name) + struct.pack(">HH", 1, 1)
    # the answer's owner as a compression pointer to the question's name
    owner = b"\xc0\x0c" if question else encode(name)
    body += owner + struct.pack(">HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(address)
    if other:
        body += encode(other[0]) + struct.pack(">HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(other[1])
    return struct.pack(">HHHHHH", 0, 0x8400, 1 if question else 0, answers, 0, 0) + body


def test_a_reply_is_read_for_the_name_asked_about():
    reply = _reply("ninaivu.local", "192.168.0.229", other=("printer.local", "192.168.0.9"))
    assert netcheck.addresses_in(reply, "ninaivu.local") == ["192.168.0.229"]
    assert netcheck.addresses_in(_reply("ninaivu.local", "10.0.0.2", question=False),
                                 "NINAIVU.local") == ["10.0.0.2"]


def test_a_broken_reply_is_nothing_rather_than_an_error():
    assert netcheck.addresses_in(b"\x00\x01\x02", "ninaivu.local") == []


def test_a_hosts_file_line_is_found(tmp_path, monkeypatch):
    # Windows reads %SystemRoot%\System32\drivers\etc\hosts; elsewhere /etc/hosts
    # comes through the stand-in Path below.
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    hosts = tmp_path / "System32" / "drivers" / "etc" / "hosts"
    hosts.parent.mkdir(parents=True)
    hosts.write_text("127.0.0.1 localhost\n127.0.0.1 ninaivu.local ninaivu-admin.local  # Ninaivu\n")
    real = Path

    def fake(*parts):
        text = str(real(*parts))
        return hosts if text.endswith("hosts") else real(*parts)
    monkeypatch.setattr(netcheck, "Path", fake)
    assert netcheck.hosts_file_entry("ninaivu.local") == "127.0.0.1"
    assert netcheck.hosts_file_entry("other.local") is None


def test_no_advice_points_at_a_script_that_is_not_shipped():
    """tools/allow-network.bat was removed; netcheck still told people to
    double-click it (see test_netinfo for the same rule in the server)."""
    assert not (ROOT / "tools" / "allow-network.bat").exists()
    assert "allow-network.bat" not in (ROOT / "tools" / "netcheck.py").read_text(encoding="utf-8")
