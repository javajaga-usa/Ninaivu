"""Choosing the family app's port: busy is not the same as not allowed.

macOS lets an ordinary user bind a port below 1024 on every address but not on
127.0.0.1 alone. Both the launcher and the server asked only on 127.0.0.1, so
443 and the 39 ports after it all read as busy, and the family app landed on a
random port. https://ninaivu.local — the address every phone had from Windows —
answered nothing, and the family app read as offline.
"""
import errno
import importlib.util
from pathlib import Path

import pytest

import ninaivu.__main__ as server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """The wait for the preferred port is for a real restart, not a test."""
    monkeypatch.setattr(server, "PREFERRED_WAIT", 0)


def launcher():
    spec = importlib.util.spec_from_file_location("ninaivu_start", ROOT / "launcher" / "start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mac_sockets(busy=()):
    """socket.socket as macOS answers an ordinary user."""
    class Socket:
        def __init__(self, *args, **kwargs):
            self.port = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            host, port = address
            if port == 0:
                self.port = 50123
                return
            if port < 1024 and host not in ("0.0.0.0", "::", ""):
                raise PermissionError(errno.EACCES, "Permission denied")
            if port in busy:
                raise OSError(errno.EADDRINUSE, "Address already in use")
            self.port = port

        def getsockname(self):
            return ("127.0.0.1", self.port)
    return Socket


@pytest.mark.parametrize("choose", ["server", "launcher"])
def test_443_is_chosen_when_nothing_holds_it(monkeypatch, choose):
    module = server if choose == "server" else launcher()
    monkeypatch.setattr(module.socket, "socket", mac_sockets())
    pick = module.pick_port if choose == "server" else module.find_port
    assert pick("0.0.0.0", 443) == 443


@pytest.mark.parametrize("choose", ["server", "launcher"])
def test_a_port_that_is_really_taken_is_still_passed_over(monkeypatch, choose):
    module = server if choose == "server" else launcher()
    monkeypatch.setattr(module.socket, "socket", mac_sockets(busy={443}))
    pick = module.pick_port if choose == "server" else module.find_port
    assert pick("0.0.0.0", 443) == 444


@pytest.mark.parametrize("choose", ["server", "launcher"])
def test_local_only_does_not_take_a_port_it_cannot_bind(monkeypatch, choose):
    """Kept to this computer, the server binds 127.0.0.1, where 443 is refused."""
    module = server if choose == "server" else launcher()
    monkeypatch.setattr(module.socket, "socket", mac_sockets())
    pick = module.pick_port if choose == "server" else module.find_port
    assert pick("127.0.0.1", 443) >= 1024


def test_a_restarts_closed_connections_do_not_make_a_port_busy():
    """A phone that had just been connected leaves the port in TIME_WAIT. The
    server binds with SO_REUSEADDR and takes it; asked without, 443 read as busy
    and the family app moved to 444 on every restart."""
    import socket as real
    import sys

    if sys.platform == "win32":
        return
    listener = real.socket()
    listener.setsockopt(real.SOL_SOCKET, real.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = real.create_connection(("127.0.0.1", port))
    accepted, _ = listener.accept()
    accepted.close()                   # the server closes first: TIME_WAIT is its
    client.close()
    listener.close()
    assert server.port_is_free("127.0.0.1", port) is True


def test_a_port_something_listens_on_is_still_busy():
    import socket as real

    listener = real.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        assert server.port_is_free("127.0.0.1", listener.getsockname()[1]) is False
    finally:
        listener.close()
