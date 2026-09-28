"""Choosing the media folder.

The bug this file exists for: an admin could not point Ninaivu at a new folder,
because the picker only ever allowed the folder Ninaivu was started with. The
console is behind an admin sign-in, so it may browse the machine; what it must
still refuse is turning a *system* directory into a media library.
"""

from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu.server import auth
from ninaivu import build_services, create_admin_app
from ninaivu.server.config import default_browse_roots, is_browsable, is_forbidden_root


# ---------------------------------------------------------------------------
# Path rules — pure, and testable on any platform
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "C:/Master",                  # the exact path from the bug report
    "C:\\Master",
    "C:/Master/Photos",
    "D:/Photos",
    "/home/alex/Pictures",
    "/mnt/nas/family",
    "/Volumes/Backup/Photos",
])
def test_ordinary_media_folders_are_allowed(path):
    assert is_browsable(Path(path))
    assert not is_forbidden_root(Path(path))


@pytest.mark.parametrize("path", [
    "/", "/etc", "/usr", "/var", "/boot", "/sys", "/proc",
    "/private/etc", "/private/var",
    "C:\\", "C:/", "C:", "C:/Windows", "C:\\Program Files",
    "c:/programdata",
])
def test_system_folders_cannot_become_a_library(path):
    assert is_forbidden_root(Path(path))


def test_a_system_folder_that_is_a_symlink_is_refused_by_either_name(monkeypatch):
    """On a Mac "/etc" is a symlink to "/private/etc", and on a Linux with a
    merged /usr "/bin" is one to "/usr/bin". The guard resolved the path and
    looked up only where it led, so "/etc" was accepted as a library on a Mac.
    The links are faked so the bug shows on any host, not only on those."""
    links = {"/etc": "/private/etc", "/var": "/private/var", "/bin": "/usr/bin"}
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        target = links.get(str(self))
        return Path(target) if target else real_resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    for typed in links:
        assert is_forbidden_root(Path(typed)), typed


@pytest.mark.parametrize("path", ["/proc", "/proc/1", "/sys/kernel", "/dev/shm"])
def test_kernel_filesystems_are_not_even_browsable(path):
    assert not is_browsable(Path(path))


def test_drive_roots_are_browsable_but_not_selectable():
    """You have to pass through C:\\ to reach C:\\Master."""
    assert is_browsable(Path("C:/"))
    assert is_forbidden_root(Path("C:/"))


def test_windows_paths_are_matched_case_and_separator_insensitively():
    for variant in ("C:/WINDOWS", "c:\\windows", "C:\\Windows\\"):
        assert is_forbidden_root(Path(variant)), variant


def test_default_browse_roots_start_at_home():
    roots = default_browse_roots()
    assert roots
    assert roots[0] == Path.home()


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------

@pytest.fixture()
def console(scanned):
    """The admin console, signed in, with only the sample library configured."""
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    return login(app.test_client(), *ADMIN), cfg


def test_admin_can_browse_outside_the_configured_root(console, tmp_path):
    """The regression: this used to be a 403."""
    client, cfg = console
    elsewhere = tmp_path / "Master"
    elsewhere.mkdir()

    response = client.get(f"/api/library/browse?path={elsewhere}")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["path"] == str(elsewhere)


def test_admin_can_set_a_folder_outside_the_configured_root(console, tmp_path):
    client, cfg = console
    elsewhere = tmp_path / "Master"
    elsewhere.mkdir()

    response = client.post("/api/library/root", json={"path": str(elsewhere)})
    assert response.status_code == 200, response.get_json()
    assert cfg.active_root == str(elsewhere)


def test_browse_offers_shortcuts_to_home_and_drives(console):
    client, _ = console
    data = client.get("/api/library/browse").get_json()
    assert data["shortcuts"], "the picker must offer somewhere to start"
    assert any(s["path"] == str(Path.home()) for s in data["shortcuts"])


def test_browse_can_climb_to_the_parent(console, tmp_path):
    client, _ = console
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    data = client.get(f"/api/library/browse?path={nested}").get_json()
    assert data["parent"] == str(tmp_path / "a")


def test_browse_reports_whether_this_folder_is_selectable(console, tmp_path):
    client, _ = console
    ok = client.get(f"/api/library/browse?path={tmp_path}").get_json()
    assert ok["selectable"] is True

    system = client.get("/api/library/browse?path=/etc").get_json()
    assert system["selectable"] is False, "browsable, but not a valid library"


def test_setting_a_system_folder_is_refused_with_advice(console):
    client, _ = console
    response = client.post("/api/library/root", json={"path": "/etc"})
    assert response.status_code == 403
    error = response.get_json()["error"]
    assert "system folder" in error
    assert "C:\\" in error or "media" in error


@pytest.mark.parametrize("path", ["/private/etc", "/private/var"])
def test_macos_system_folders_are_refused_by_their_real_names(console, path):
    """Where the picker lands after opening /private on a Mac. Absent on other
    hosts, and refused there too, as typed."""
    client, _ = console
    browsed = client.get(f"/api/library/browse?path={path}").get_json()
    assert browsed["selectable"] is False

    response = client.post("/api/library/root", json={"path": path})
    assert response.status_code == 403
    assert "system folder" in response.get_json()["error"]


def test_missing_folder_says_so_plainly(console, tmp_path):
    client, _ = console
    response = client.post("/api/library/root", json={"path": str(tmp_path / "nope")})
    assert response.status_code == 400
    assert "does not exist" in response.get_json()["error"]


def test_a_file_is_not_a_folder(console, tmp_path):
    client, _ = console
    target = tmp_path / "photo.jpg"
    target.write_text("x")
    response = client.post("/api/library/root", json={"path": str(target)})
    assert response.status_code == 400
    assert "not a folder" in response.get_json()["error"]


def test_kernel_filesystems_are_refused_by_browse(console):
    client, _ = console
    assert client.get("/api/library/browse?path=/proc").status_code == 403


# ---------------------------------------------------------------------------
# --lock-roots, for when the console is exposed
# ---------------------------------------------------------------------------

@pytest.fixture()
def locked_console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.lock_roots = True
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    return login(create_admin_app(services).test_client(), *ADMIN), cfg


def test_lock_roots_confines_the_picker(locked_console, tmp_path):
    client, cfg = locked_console
    elsewhere = tmp_path / "Master"
    elsewhere.mkdir()

    assert client.get(f"/api/library/browse?path={elsewhere}").status_code == 403
    response = client.post("/api/library/root", json={"path": str(elsewhere)})
    assert response.status_code == 403
    error = response.get_json()["error"]
    assert "--lock-roots" in error
    assert "--allow" in error, "the refusal must say how to fix it"


def test_lock_roots_still_allows_the_configured_root(locked_console):
    client, cfg = locked_console
    assert client.get(
        f"/api/library/browse?path={cfg.active_root}").status_code == 200


def test_lock_roots_is_off_by_default(cfg):
    assert cfg.lock_roots is False


# ---------------------------------------------------------------------------
# The family app has no library management at all
# ---------------------------------------------------------------------------

def test_library_management_is_console_only(app, scanned):
    """These live on port 3000 now; the family app never routes them."""
    from ninaivu import create_home_app

    cfg, conn, _ = scanned
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    home = login(create_home_app(services).test_client(), *ADMIN)

    assert home.get("/api/library/browse").status_code == 404
    assert home.post("/api/library/root", json={"path": "/tmp"}).status_code == 404
    assert home.post("/api/scan", json={}).status_code == 404


def test_family_page_does_not_offer_a_folder_picker(app):
    page = app.test_client().get("/").get_data(as_text=True)
    assert "folder-modal" not in page
    assert "pick-folder" not in page
