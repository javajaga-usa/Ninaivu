"""Reading a phone, which Windows shows as a folder and is not one.

The COM half of this cannot be tested anywhere but a Windows machine with a
device plugged into it, so what is tested here is everything around it: the
path grammar, the walk, the copy loop's decisions, and the promise that
nothing is ever written to or deleted from the device.

The shell is replaced by a fake that behaves the way the real one does,
including the awkward parts — a copy that returns immediately and finishes
later, and sizes reported as localised text.
"""

import os
from pathlib import Path

import pytest

from ninaivu.utils import devices


# ---------------------------------------------------------------------------
# The path grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "This PC\\Apple iPhone\\Internal Storage",
    "this pc\\Galaxy\\Phone\\DCIM",
    "Computer\\Nikon D750\\DCIM",
    "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
])
def test_a_device_path_is_recognised(path):
    assert devices.looks_like_device_path(path) is True


@pytest.mark.parametrize("path", ["C:\\Master", "\\\\NAS\\Photos", "/srv/pics", ""])
def test_an_ordinary_path_is_not(path):
    assert devices.looks_like_device_path(path) is False


def test_a_path_splits_into_device_and_route():
    device, parts = devices.split_device_path(
        "This PC\\Apple iPhone\\Internal Storage\\DCIM\\100APPLE")
    assert device == "Apple iPhone"
    assert parts == ["Internal Storage", "DCIM", "100APPLE"]


def test_forward_slashes_and_quotes_are_tolerated():
    """People paste from the address bar, and it does not always come clean."""
    device, parts = devices.split_device_path('"This PC/Apple iPhone/DCIM"')
    assert device == "Apple iPhone" and parts == ["DCIM"]


@pytest.mark.parametrize("text,expected", [
    ("3,492 KB", 3492 * 1024),      # English thousands separator
    ("3.492 KB", 3492 * 1024),      # the same number in German
    ("12 GB", 12 * 1024 ** 3),
    ("", 0), ("rubbish", 0),
])
def test_localised_sizes_are_read(text, expected):
    assert devices._parse_size(text) == expected


# ---------------------------------------------------------------------------
# A fake shell, behaving badly in the ways the real one does
# ---------------------------------------------------------------------------

class FakeItem:
    def __init__(self, name, folder=None, size="1,024 KB"):
        self.Name = name
        self._folder = folder
        self.size = size
        self.Path = f"::{{fake}}\\{name}" if folder is not None else name

    @property
    def IsFolder(self):
        return self._folder is not None

    @property
    def GetFolder(self):
        return self._folder


class FakeFolder:
    def __init__(self, items, destination=None):
        self._items = items
        self.destination = destination
        self.copied = []

    def Items(self):
        return list(self._items)

    def GetDetailsOf(self, item, column):
        return {0: item.Name, 1: getattr(item, "size", ""), 2: "File", 3: "01/01/2026"}.get(column, "")

    def CopyHere(self, item, flags):
        """Like the real one: returns at once, and the file appears later."""
        self.copied.append((item.Name, flags))
        if self.destination:
            (Path(self.destination) / item.Name).write_bytes(b"x" * 32)


class FakeShell:
    def __init__(self, tree, destination=None):
        self.tree = tree
        self.destination = destination

    def NameSpace(self, what):
        if what == devices.SSF_DRIVES:
            return self.tree
        return FakeFolder([], destination=self.destination)


@pytest.fixture()
def phone(tmp_path, monkeypatch):
    """An iPhone with two camera-roll folders, and somewhere to copy to."""
    inner = FakeFolder([FakeItem("IMG_0001.HEIC"), FakeItem("IMG_0002.HEIC")])
    dcim = FakeFolder([FakeItem("100APPLE", inner)])
    storage = FakeFolder([FakeItem("DCIM", dcim)])
    computer = FakeFolder([
        FakeItem("Local Disk (C:)", FakeFolder([])),
        FakeItem("Apple iPhone", storage),
    ])
    computer._items[0].Path = "C:\\"
    landing = tmp_path / "landing"
    monkeypatch.setattr(devices, "unavailable_reason", lambda: None)
    monkeypatch.setattr(devices, "_shell",
                        lambda: FakeShell(computer, destination=landing))
    return landing


def test_a_drive_is_not_listed_as_a_device(phone):
    """A drive has a letter; a device does not. That is the whole test."""
    names = [d["name"] for d in devices.list_devices()]
    assert "Apple iPhone" in names
    assert "Local Disk (C:)" not in names


def test_a_folder_on_the_device_can_be_listed(phone):
    entries = devices.list_folder("This PC\\Apple iPhone\\DCIM\\100APPLE")
    assert [e["name"] for e in entries] == ["IMG_0001.HEIC", "IMG_0002.HEIC"]
    assert all(not e["is_folder"] for e in entries)
    assert all(e["size"] > 0 for e in entries)


def test_a_missing_folder_says_the_phone_may_be_locked(phone):
    """The commonest cause by far, and invisible otherwise."""
    with pytest.raises(FileNotFoundError) as caught:
        devices.list_folder("This PC\\Apple iPhone\\Nope")
    assert "unlock" in str(caught.value).lower()


def test_the_walk_descends_into_every_camera_roll_folder(phone):
    found = [e["name"] for e in devices.walk("This PC\\Apple iPhone\\DCIM")]
    assert sorted(found) == ["IMG_0001.HEIC", "IMG_0002.HEIC"]


def test_the_walk_can_be_called_off(phone):
    stop = {"now": False}
    seen = []
    for entry in devices.walk("This PC\\Apple iPhone\\DCIM",
                              should_stop=lambda: stop["now"]):
        seen.append(entry)
        stop["now"] = True
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Copying off
# ---------------------------------------------------------------------------

def test_files_are_copied_into_a_real_folder(phone):
    result = devices.copy_out("This PC\\Apple iPhone\\DCIM", phone)
    assert result["copied"] == 2
    assert result["failed"] == 0
    assert sorted(p.name for p in Path(phone).iterdir()) == \
        ["IMG_0001.HEIC", "IMG_0002.HEIC"]


def test_a_file_already_brought_over_is_not_fetched_twice(phone):
    """An interrupted import must cost the remainder, not the whole thing."""
    devices.copy_out("This PC\\Apple iPhone\\DCIM", phone)
    again = devices.copy_out("This PC\\Apple iPhone\\DCIM", phone)
    assert again["copied"] == 0
    assert again["skipped"] == 2


def test_a_copy_reports_as_it_goes(phone):
    seen = []
    devices.copy_out("This PC\\Apple iPhone\\DCIM", phone,
                     progress=lambda n, b, name: seen.append(name))
    assert seen == ["IMG_0001.HEIC", "IMG_0002.HEIC"]


def test_a_copy_stops_when_asked(phone):
    stop = {"now": False}

    def progress(n, b, name):
        stop["now"] = True

    result = devices.copy_out("This PC\\Apple iPhone\\DCIM", phone,
                              progress=progress,
                              should_stop=lambda: stop["now"])
    assert result["copied"] == 1


def test_nothing_is_ever_written_to_the_device():
    """A phone is not a backup target, and this module must not treat it as one.

    Asserted against the source because the guarantee is the absence of a
    call, and absences are what get added back by accident.
    """
    import inspect

    source = inspect.getsource(devices)
    for forbidden in ("MoveHere", "NewFolder", "InvokeVerb", ".Delete("):
        assert forbidden not in source, f"{forbidden} would write to the device"
    # One actual call, and its receiver is the local destination folder.
    calls = [line.strip() for line in source.splitlines()
             if ".CopyHere(" in line and not line.strip().startswith("#")]
    assert calls == ["destination.CopyHere(item, _COPY_FLAGS)"], calls


# ---------------------------------------------------------------------------
# Off Windows
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="the not-Windows case")
def test_elsewhere_it_says_so_rather_than_failing():
    reason = devices.unavailable_reason()
    assert reason and "Windows" in reason
    assert devices.available() is False


@pytest.mark.skipif(os.name == "nt", reason="the not-Windows case")
def test_asking_anyway_raises_something_readable():
    with pytest.raises(RuntimeError) as caught:
        devices.list_devices()
    assert "Windows" in str(caught.value)


def test_the_console_endpoint_reports_rather_than_erroring(as_admin):
    """A machine that cannot do this should say so calmly, not return a 500."""
    body = as_admin.get("/api/devices").get_json()
    assert "available" in body
    if not body["available"]:
        assert body["reason"]
    assert isinstance(body["devices"], list)


def test_devices_are_console_only(scanned):
    from ninaivu import build_services, create_home_app

    cfg, _, _ = scanned
    client = create_home_app(build_services(cfg)).test_client()
    assert client.get("/api/devices").status_code == 404


def test_folder_exists_on_device(phone):
    assert devices.folder_exists("This PC\\Apple iPhone\\DCIM\\100APPLE") is True
    assert devices.folder_exists("This PC\\Apple iPhone\\Nope") is False


def test_validate_job_with_device(phone, tmp_path):
    from ninaivu.archive.safety import validate_job

    dest = tmp_path / "archive"
    dest.mkdir()
    # Existing device folder is accepted
    assert validate_job(["This PC\\Apple iPhone\\DCIM\\100APPLE"], str(dest)) == []
    # Missing device folder reports friendly advice
    problems = validate_job(["This PC\\Apple iPhone\\Nope"], str(dest))
    assert len(problems) == 1
    assert "not on the device" in problems[0]


def test_shortcuts_include_connected_devices(phone):
    from ninaivu.api.api_library import _shortcuts
    from ninaivu.server.config import Config

    shortcuts = _shortcuts(Config())
    assert any(s["path"] == "This PC\\Apple iPhone" for s in shortcuts)


def test_api_library_browse_device_path(phone, as_admin):
    res = as_admin.get("/api/library/browse?path=This PC\\Apple iPhone\\DCIM")
    assert res.status_code == 200
    data = res.get_json()
    assert data["path"] == "This PC\\Apple iPhone\\DCIM"
    assert len(data["dirs"]) == 1
    assert data["dirs"][0]["name"] == "100APPLE"
    assert data["selectable"] is True

