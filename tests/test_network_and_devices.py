"""Folders that are not on this disk: shares, mapped drives, and phones.

Three different failures were all reported as "the Archive tab will not take
my folder", and only two of them are the same kind of problem:

* a mapped network drive was hidden from the picker on purpose;
* a share that is present but refuses this account says "does not exist";
* a phone over USB is not a folder at all and never can be.

The last one cannot be fixed by handling paths better, so what it gets is an
answer that says so.
"""

import os

import pytest

from ninaivu.archive import safety
from ninaivu.server.config import shell_namespace_hint


# ---------------------------------------------------------------------------
# A phone is not a folder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "This PC\\Apple iPhone\\Internal Storage",
    "Computer\\Nikon D750\\DCIM",
    "this pc\\Galaxy S24\\Phone",
    "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
])
def test_a_shell_location_is_recognised(path):
    hint = shell_namespace_hint(path)
    assert hint, f"{path} should be explained, not treated as a missing folder"


@pytest.mark.parametrize("path", [
    "C:\\Master", "/home/someone/Pictures", "\\\\NAS\\Photos", "Z:\\Pictures", "",
])
def test_a_real_path_is_left_alone(path):
    assert shell_namespace_hint(path) is None


def test_the_phone_names_itself_in_the_explanation():
    hint = shell_namespace_hint("This PC\\Apple iPhone\\Internal Storage")
    assert "Apple iPhone" in hint


def test_the_archive_explains_a_phone_rather_than_calling_it_missing():
    problem = safety._why_not_a_folder(
        "This PC\\Apple iPhone\\Internal Storage", "Source")
    assert "does not exist" not in problem
    assert "Apple iPhone" in problem
    assert "Import" in problem or "Copy" in problem


def test_a_phone_as_the_destination_is_refused_with_the_same_explanation():
    problems = safety.validate_job(
        [{"path": os.getcwd(), "types": ["photos"]}],
        "This PC\\Apple iPhone\\Internal Storage")
    assert any("Apple iPhone" in p for p in problems)


# ---------------------------------------------------------------------------
# A share that is there but will not let us in
# ---------------------------------------------------------------------------

def test_an_unreachable_share_is_not_reported_as_a_typo():
    """"Does not exist" sends somebody to check their spelling for an hour."""
    problem = safety._why_not_a_folder("\\\\NAS\\Photos", "Source")
    assert "network" in problem.lower()
    assert "credential" in problem.lower() or "sign-in" in problem.lower() \
        or "signed in" in problem.lower()


def test_a_plain_missing_folder_still_says_so():
    """The common case must not be buried under network advice."""
    problem = safety._why_not_a_folder("C:\\definitely-not-here", "Source")
    assert "does not exist" in problem
    assert "network" not in problem.lower()


def test_a_unc_path_is_recognised_as_a_network_location():
    assert safety._is_network("\\\\NAS\\Photos") is True
    assert safety._is_network("//NAS/Photos") is True
    assert safety._is_network("C:\\Master") is False


# ---------------------------------------------------------------------------
# Mapped network drives are offered again
# ---------------------------------------------------------------------------

def test_network_drives_are_listed_without_being_touched(as_admin, monkeypatch):
    """Listing one must cost nothing: that is why they were dropped before.

    A mapped drive whose server is asleep blocks until SMB gives up, so the
    shortcut list may name a network drive but must never stat it.
    """
    from pathlib import Path as _Path

    # The library picker moved into api_library when api.py was split; the
    # routes and their behaviour did not.
    from ninaivu.api import api_library
    from ninaivu.server import config

    remote = "Z:\\"
    monkeypatch.setattr(config, "network_drives", lambda: {remote})
    monkeypatch.setattr(api_library, "_browse_roots", lambda cfg: [_Path(remote)])

    real_is_dir = _Path.is_dir

    def guarded(self):
        if str(self) == remote:
            raise AssertionError("a network drive must not be probed")
        return real_is_dir(self)

    monkeypatch.setattr(_Path, "is_dir", guarded)
    # Called through the endpoint so the app context exists.
    body = as_admin.get("/api/library/browse").get_json()
    names = [s["name"] for s in body["shortcuts"]]
    assert any("network" in n for n in names), names


def test_the_drive_lister_no_longer_filters_network_drives():
    import inspect

    from ninaivu.server import config

    source = inspect.getsource(config.windows_drives)
    assert "DRIVE_CDROM" in source
    assert "kind in (DRIVE_REMOTE, DRIVE_CDROM)" not in source, \
        "network drives should no longer be skipped"


def test_network_drives_helper_is_safe_off_windows():
    """It must return an empty set rather than raising on Linux or macOS."""
    from ninaivu.server.config import network_drives

    assert isinstance(network_drives(), set)


# ---------------------------------------------------------------------------
# The picker
# ---------------------------------------------------------------------------

def test_the_picker_explains_a_phone_instead_of_a_bare_error(as_admin):
    response = as_admin.get(
        "/api/library/browse?path=This PC\\Apple iPhone\\Internal Storage")
    assert response.status_code == 400
    body = response.get_json()
    assert "Apple iPhone" in body["error"]
    assert "Import" in body["error"] or "Copy" in body["error"]


def test_the_picker_still_browses_an_ordinary_folder(as_admin, tmp_path):
    (tmp_path / "somewhere").mkdir()
    response = as_admin.get(f"/api/library/browse?path={tmp_path}")
    assert response.status_code == 200
    assert any(d["name"] == "somewhere" for d in response.get_json()["dirs"])
