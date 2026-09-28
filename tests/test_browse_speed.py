"""The folder picker must not wait on drives it does not need.

The obvious way to find Windows drive letters — `Path("Z:\\").exists()` for
every letter — blocks on exactly the drives that are slow: a mapped network
share whose server is off waits for SMB to give up, an empty optical drive
spins up, a card reader with no card stalls. Twenty-four of those ran on every
folder the picker opened, which is why it sat on "Loading…".
"""

from pathlib import Path

import pytest

from ninaivu.server import config as cfg_mod
from ninaivu.server.config import (DRIVE_CDROM, DRIVE_FIXED, DRIVE_REMOTE,
                           DRIVE_REMOVABLE, default_browse_roots, windows_drives)


class FakeKernel32:
    """A machine with C:, D:, an offline network Z:, and an empty card reader."""

    TYPES = {"C:\\": DRIVE_FIXED, "D:\\": DRIVE_FIXED,
             "E:\\": DRIVE_CDROM, "F:\\": DRIVE_REMOVABLE, "Z:\\": DRIVE_REMOTE}

    def __init__(self, card_present=False):
        self.touched = []
        self.card_present = card_present

    def GetLogicalDrives(self):                   # noqa: N802 — Windows name
        mask = 0
        for root in self.TYPES:
            mask |= 1 << (ord(root[0]) - ord("A"))
        return mask

    def GetDriveTypeW(self, root):                # noqa: N802
        return self.TYPES.get(root, DRIVE_FIXED)

    def GetVolumeInformationW(self, root, *rest):  # noqa: N802
        self.touched.append(root)
        return 1 if self.card_present else 0


@pytest.fixture()
def windows(monkeypatch):
    def install(kernel):
        fake = type("ctypes", (), {"windll": type("w", (), {"kernel32": kernel})()})()
        # Deliberately *not* patching os.name: pathlib reads it globally and
        # would start minting WindowsPath objects on this machine. windows_drives()
        # asks the kernel directly, so it is testable as it stands.
        monkeypatch.setitem(__import__("sys").modules, "ctypes", fake)
        cfg_mod._BROWSE_CACHE.update(at=0.0, roots=None)
        return kernel
    yield install
    cfg_mod._BROWSE_CACHE.update(at=0.0, roots=None)


def test_only_usable_drives_are_offered(windows):
    """Network drives are offered now; optical and empty card readers are not.

    They used to be filtered out because a mapped drive whose server is asleep
    blocks until SMB gives up. That reasoning holds — and the conclusion did
    not: some households keep their photographs on a NAS, and hiding the drive
    meant the Archive tab could not see them at all. Listing costs nothing
    (see the next test); only touching one is expensive, and nothing here
    touches it.
    """
    windows(FakeKernel32())
    assert [str(d) for d in windows_drives()] == ["C:\\", "D:\\", "Z:\\"]


def test_the_network_drive_is_named_as_one(windows):
    """So the picker can label it and explain itself when it does not answer."""
    from ninaivu.server.config import network_drives

    windows(FakeKernel32())
    assert network_drives() == {"Z:\\"}


def test_an_offline_network_drive_is_never_touched(windows):
    kernel = windows(FakeKernel32())
    windows_drives()
    assert "Z:\\" not in kernel.touched, (
        "the network drive was probed — this is the one that hangs")


def test_an_optical_drive_is_not_spun_up(windows):
    kernel = windows(FakeKernel32())
    windows_drives()
    assert "E:\\" not in kernel.touched


def test_a_card_reader_is_asked_without_waiting_and_skipped_when_empty(windows):
    kernel = windows(FakeKernel32(card_present=False))
    assert "F:\\" not in [str(d) for d in windows_drives()]
    assert kernel.touched == ["F:\\"], (
        "the removable drive should be asked exactly once, and cheaply")


def test_a_card_reader_with_a_card_in_it_is_offered(windows):
    windows(FakeKernel32(card_present=True))
    assert "F:\\" in [str(d) for d in windows_drives()]


def test_nothing_iterates_every_letter_of_the_alphabet(windows):
    """A drive letter that does not exist must cost nothing at all."""
    kernel = windows(FakeKernel32())
    windows_drives()
    absent = {f"{c}:\\" for c in "ABGHIJKLMNOPQRSTUVWXY"}
    assert not absent & set(kernel.touched)


def test_the_answer_is_cached_between_navigations(monkeypatch):
    """The picker asks on every click; the answer changes when hardware does."""
    cfg_mod._BROWSE_CACHE.update(at=0.0, roots=None)
    calls = []
    real = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: calls.append(self) or real(self))

    default_browse_roots()
    first = len(calls)
    assert first > 0
    default_browse_roots()
    assert len(calls) == first, "the second navigation walked the disk again"


def test_the_cache_expires_so_a_new_drive_turns_up(monkeypatch):
    cfg_mod._BROWSE_CACHE.update(at=0.0, roots=None)
    default_browse_roots()
    cached_at = cfg_mod._BROWSE_CACHE["at"]
    monkeypatch.setattr(cfg_mod.time, "time",
                        lambda: cached_at + cfg_mod.BROWSE_CACHE_SECONDS + 1)
    calls = []
    real = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: calls.append(self) or real(self))
    default_browse_roots()
    assert calls, "the cache never expired, so a drive plugged in stays invisible"


def test_a_machine_without_ctypes_degrades_quietly(monkeypatch):
    class Broken:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setitem(__import__("sys").modules, "ctypes", Broken())
    assert windows_drives() == []


# --- macOS ------------------------------------------------------------------

ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.42 netmask 0xffffff00 broadcast 192.168.1.255
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 100.85.1.2 --> 100.85.1.2 netmask 0xffffffff
"""


@pytest.fixture()
def macos(monkeypatch):
    from ninaivu.utils import netinfo as ni

    monkeypatch.setattr(ni.sys, "platform", "darwin")
    monkeypatch.setattr(ni, "_run", lambda command:
                        ROUTE if command[0] == "route" else IFCONFIG)
    return ni


def test_macos_finds_its_adapters_without_ip_or_proc(macos):
    """macOS has neither `ip` nor /proc — the Linux branch returns nothing."""
    names = [a["name"] for a in macos.adapters()]
    assert "en0" in names and "bridge100" in names
    assert "lo0" not in names


def test_macos_picks_the_wifi_address_not_the_vm_bridge(macos):
    assert macos.best_address() == "192.168.1.42", (
        "bridge100 is UTM/Parallels and utun4 is a VPN; neither is reachable "
        "from a phone, and both look like ordinary addresses")


def test_macos_marks_only_the_routed_adapter(macos):
    routed = {a["name"] for a in macos.adapters() if a["gateway"]}
    assert routed == {"en0"}
