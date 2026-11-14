"""The Setup check page (ninaivu/server/setup_check.py): ports, drives,
Tailscale and permissions, each said with what to do. Every lookup goes
through Probes, so a Mac, a Windows PC and a Linux box are stood in for here
without being one."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.server import setup_check as sc

ROOT = Path(__file__).resolve().parents[1]
GB = 1024 ** 3


def done(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def config(tmp_path: Path, **overrides):
    library = tmp_path / "photos"
    library.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    values = dict(port=80, admin_port=3000, host="0.0.0.0", admin_host=None,
                  roots=[str(library)], state_dir=state, backup_dir="", mirror_enabled=False,
                  mirror_dir="", mugil_role="single", mugil_originals_dir="",
                  remote_access="auto", tailnet_https=True, remote_networks=[],
                  remote_hostname="", trusted_proxies=0)
    values.update(overrides)
    return SimpleNamespace(**values)


def probes(**overrides) -> sc.Probes:
    """A computer where everything is fine, unless told otherwise."""
    values = dict(
        platform="linux",
        connects=lambda host, port: True,
        lan_addresses=lambda: ["192.168.1.20"],
        exists=lambda path: Path(path).is_dir(),
        disk_usage=lambda path: (500 * GB, 1000 * GB),
        device=lambda path: 1,
        can_list=lambda path: "ok",
        can_write=lambda path: True,
        mode=lambda path: stat.S_IFDIR | 0o700,
        is_root=lambda: False,
        account=lambda: "jaga",
        run=lambda command: None,
        tailscale_cli=lambda: None,
        saved_tailnet_name=lambda state: "",
    )
    values.update(overrides)
    return sc.Probes(**values)


def by_key(checks, key):
    found = [c for c in checks if c.key == key]
    assert found, f"no {key!r} in {[c.key for c in checks]}"
    return found[0]


# --- ports -----------------------------------------------------------------

def test_a_computer_set_up_well_says_so(tmp_path):
    result = sc.run_all(config(tmp_path), probes())
    assert result["counts"]["fix"] == 0
    assert result["counts"]["warn"] == 0
    areas = {c["area"] for c in result["checks"]}
    assert areas == {"ports", "drives", "tailscale", "permissions"}
    network = next(c for c in result["checks"] if c["key"] == "network")
    assert network["title"]["text"] == "Phones at home can open the family app at http://192.168.1.20"
    # Sent as a template and its values, for the console to translate.
    assert network["title"]["said"] == "Phones at home can open the family app at {url}"
    assert network["title"]["params"] == {"url": "http://192.168.1.20"}


def test_a_port_that_does_not_answer_is_a_fix_on_the_server_page(tmp_path):
    checks = sc.check_ports(config(tmp_path), probes(connects=lambda h, p: p != 80))
    family = by_key(checks, "family-port")
    assert family.state == "fix" and family.page == "server"
    assert by_key(checks, "console-port").state == "ok"


def test_a_port_stepped_past_says_which_one_was_taken(tmp_path):
    checks = sc.check_ports(config(tmp_path, port=81), probes(),
                            asked={"port": 80, "admin_port": 3000})
    family = by_key(checks, "family-port")
    assert family.state == "warn"
    assert family.title["text"] == "Port 80 was taken, so the family app is on port 81."
    assert by_key(checks, "console-port").state == "ok"


def test_only_this_computer_is_said_with_the_switch_that_changes_it(tmp_path):
    checks = sc.check_ports(config(tmp_path, host="127.0.0.1"), probes())
    network = by_key(checks, "network")
    assert network.state == "warn" and network.page == "server"
    assert "Network access" in network.fix["text"]


def test_https_addresses_leave_out_the_port_that_is_https_own(tmp_path):
    checks = sc.check_ports(config(tmp_path, port=443), probes(), scheme="https")
    assert by_key(checks, "network").title["params"]["url"] == "https://192.168.1.20"


def test_mac_firewall_blocking_everything_is_a_fix(tmp_path):
    def run(command):
        if "--getglobalstate" in command:
            return done("Firewall is enabled. (State = 1)")
        if "--getblockall" in command:
            return done("Firewall has block all state set to enabled.")
        return None
    firewall = by_key(sc.check_ports(config(tmp_path), probes(platform="darwin", run=run)), "firewall")
    assert firewall.state == "fix"
    assert "Block all incoming connections" in firewall.fix["text"]


def test_mac_firewall_off_is_fine(tmp_path):
    run = lambda command: done("Firewall is disabled. (State = 0)")    # noqa: E731
    assert by_key(sc.check_ports(config(tmp_path), probes(platform="darwin", run=run)),
                  "firewall").state == "ok"


def test_windows_without_a_rule_gets_the_command_to_add_one(tmp_path):
    def run(command):
        if command[0] == "netsh":
            return done("No rules match the specified criteria.")
        return done("Private")
    firewall = by_key(sc.check_ports(config(tmp_path, port=8080), probes(platform="win32", run=run)),
                      "firewall")
    assert firewall.state == "fix"
    assert "localport=8080" in firewall.command


def test_windows_rule_for_8080_does_not_cover_80(tmp_path):
    def run(command):
        if command[0] == "netsh":
            return done("Rule Name: Ninaivu\nLocalPort: 8080\n")
        return done("Private")
    assert by_key(sc.check_ports(config(tmp_path), probes(platform="win32", run=run)),
                  "firewall").state == "fix"


def test_windows_public_network_is_a_fix(tmp_path):
    def run(command):
        if command[0] == "netsh":
            return done("LocalPort: 80,3000")
        return done("Public")
    checks = sc.check_ports(config(tmp_path), probes(platform="win32", run=run))
    assert by_key(checks, "network-profile").state == "fix"
    assert by_key(checks, "firewall").state == "ok"


def test_linux_ufw_on_gives_the_allow_command(tmp_path):
    run = lambda command: done("ENABLED=yes") if command[0] == "sh" else None    # noqa: E731
    firewall = by_key(sc.check_ports(config(tmp_path), probes(run=run)), "firewall")
    assert firewall.state == "warn" and firewall.command == "sudo ufw allow 80/tcp"


# --- drives ----------------------------------------------------------------

def test_a_drive_that_is_not_plugged_in_is_said_by_its_name(tmp_path):
    cfg = config(tmp_path, roots=["/Volumes/J Red/Photos"])
    checks = sc.check_drives(cfg, probes(platform="darwin"))
    missing = by_key(checks, "there:/Volumes/J Red/Photos")
    assert missing.state == "fix"
    assert missing.title["text"] == "The drive J Red is not connected."
    assert missing.page == "library"


def test_a_folder_gone_from_a_drive_that_is_there_is_not_called_unplugged(tmp_path):
    cfg = config(tmp_path, roots=["/Volumes/J Red/Photos"])
    exists = lambda path: path == "/Volumes/J Red" or Path(path).is_dir()    # noqa: E731
    missing = by_key(sc.check_drives(cfg, probes(platform="darwin", exists=exists)),
                     "there:/Volumes/J Red/Photos")
    assert missing.title["text"] == "A folder Ninaivu uses is not there."


def test_windows_drive_letter_is_named(tmp_path):
    cfg = config(tmp_path, roots=["E:\\Photos"])
    missing = by_key(sc.check_drives(cfg, probes(platform="win32")), "there:E:\\Photos")
    assert missing.title["params"] == {"drive": "E:"}


def test_a_nearly_full_disk_is_one_fix_for_every_folder_on_it(tmp_path):
    checks = sc.check_drives(config(tmp_path), probes(disk_usage=lambda p: (1 * GB, 1000 * GB)))
    space = [c for c in checks if c.key.startswith("space:")]
    assert len(space) == 1 and space[0].state == "fix"
    assert space[0].title["text"] == "A disk is nearly full: 1.0 GB free of 1000.0 GB."
    assert str(tmp_path / "photos") in space[0].detail["text"]
    assert str(tmp_path / "state") in space[0].detail["text"]


def test_a_disk_getting_full_is_worth_a_look(tmp_path):
    checks = sc.check_drives(config(tmp_path), probes(disk_usage=lambda p: (5 * GB, 100 * GB)))
    assert [c.state for c in checks if c.key.startswith("space:")] == ["warn"]


def test_two_disks_get_a_line_each(tmp_path):
    device = lambda path: 2 if "photos" in path else 1    # noqa: E731
    checks = sc.check_drives(config(tmp_path), probes(device=device))
    assert len([c for c in checks if c.key.startswith("space:")]) == 2


def test_copies_of_the_index_on_the_index_disk_are_worth_a_look(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    checks = sc.check_drives(config(tmp_path, backup_dir=str(backups)), probes())
    assert by_key(checks, "backup-disk").state == "warn"


def test_the_secondarys_originals_folder_is_looked_at(tmp_path):
    cfg = config(tmp_path, mugil_role="secondary", mugil_originals_dir="/mnt/n300/originals")
    missing = by_key(sc.check_drives(cfg, probes()), "there:/mnt/n300/originals")
    assert missing.title["text"] == "The drive n300 is not connected."


# --- tailscale -------------------------------------------------------------

def tailscale(status: dict | None, funnel: str = "No serve config"):
    def run(command):
        if command[1:] == ["status", "--json"]:
            return done(json.dumps(status)) if status is not None else done("", 1)
        if command[1:] == ["funnel", "status"]:
            return done(funnel)
        return None
    return run


RUNNING = {"BackendState": "Running", "Self": {"DNSName": "mini.tail1234.ts.net."},
           "CertDomains": ["mini.tail1234.ts.net"]}


def test_tailscale_not_installed_is_only_information_on_automatic(tmp_path):
    checks = sc.check_tailscale(config(tmp_path), probes())
    assert [c.state for c in checks] == ["info"]
    assert checks[0].link == sc.TAILSCALE_DOWNLOAD


def test_tailscale_chosen_but_not_installed_is_a_fix(tmp_path):
    checks = sc.check_tailscale(config(tmp_path, remote_access="tailscale"), probes())
    assert [c.state for c in checks] == ["fix"]


def test_tailscale_not_connected(tmp_path):
    p = probes(tailscale_cli=lambda: "/usr/bin/tailscale",
               run=tailscale({"BackendState": "NeedsLogin", "Self": {}}))
    assert by_key(sc.check_tailscale(config(tmp_path), p), "connected").state == "fix"


def test_tailscale_connected_with_its_certificate_gives_the_address(tmp_path):
    p = probes(tailscale_cli=lambda: "/usr/bin/tailscale", run=tailscale(RUNNING),
               saved_tailnet_name=lambda state: "mini.tail1234.ts.net")
    checks = sc.check_tailscale(config(tmp_path, port=443), p, scheme="https")
    assert {c.state for c in checks} == {"ok"}
    assert by_key(checks, "certificate").title["params"]["url"] == "https://mini.tail1234.ts.net"


def test_certificates_off_for_the_tailnet_point_at_tailscales_page(tmp_path):
    status = dict(RUNNING, CertDomains=[])
    p = probes(tailscale_cli=lambda: "/usr/bin/tailscale", run=tailscale(status))
    certificate = by_key(sc.check_tailscale(config(tmp_path), p), "certificate")
    assert certificate.state == "warn" and certificate.link == sc.TAILSCALE_HTTPS_SETTING


def test_certificate_not_fetched_yet_says_restart(tmp_path):
    p = probes(tailscale_cli=lambda: "/usr/bin/tailscale", run=tailscale(RUNNING))
    certificate = by_key(sc.check_tailscale(config(tmp_path), p), "certificate")
    assert certificate.state == "warn" and certificate.page == "server"


def test_funnel_on_is_a_fix(tmp_path):
    p = probes(tailscale_cli=lambda: "/usr/bin/tailscale",
               run=tailscale(RUNNING, funnel="https://mini.tail1234.ts.net (Funnel on)"),
               saved_tailnet_name=lambda state: "mini.tail1234.ts.net")
    funnel = by_key(sc.check_tailscale(config(tmp_path), p), "funnel")
    assert funnel.state == "fix" and funnel.command == "/usr/bin/tailscale funnel reset"


def test_tailscale_with_ninaivu_on_this_computer_only(tmp_path):
    p = probes(tailscale_cli=lambda: "/usr/bin/tailscale", run=tailscale(RUNNING),
               saved_tailnet_name=lambda state: "mini.tail1234.ts.net")
    checks = sc.check_tailscale(config(tmp_path, host="127.0.0.1"), p)
    assert by_key(checks, "listening").state == "fix"


def test_away_from_home_turned_off(tmp_path):
    checks = sc.check_tailscale(config(tmp_path, remote_access="none"), probes())
    assert [c.state for c in checks] == ["info"]


# --- permissions -----------------------------------------------------------

def test_a_mac_folder_kept_out_of_says_full_disk_access(tmp_path):
    checks = sc.check_permissions(config(tmp_path),
                                  probes(platform="darwin", can_list=lambda p: "denied"))
    read = [c for c in checks if c.key.startswith("read:")][0]
    assert read.state == "fix" and "Full Disk Access" in read.fix["text"]


def test_a_library_that_cannot_be_added_to_is_worth_a_look(tmp_path):
    def can_write(path):
        return "photos" not in path
    checks = sc.check_permissions(config(tmp_path), probes(can_write=can_write))
    write = [c for c in checks if c.key.startswith("write:")][0]
    assert write.state == "warn" and "jaga" in write.fix["text"]


def test_an_open_state_folder_gets_chmod(tmp_path):
    checks = sc.check_permissions(config(tmp_path), probes(mode=lambda p: stat.S_IFDIR | 0o755))
    private = by_key(checks, "state-private")
    assert private.state == "warn" and private.command.startswith("chmod 700 ")


def test_windows_is_not_asked_about_posix_modes(tmp_path):
    checks = sc.check_permissions(config(tmp_path), probes(platform="win32",
                                                           mode=lambda p: stat.S_IFDIR | 0o777))
    assert not [c for c in checks if c.key == "state-private"]


def test_running_as_root_is_worth_a_look(tmp_path):
    checks = sc.check_permissions(config(tmp_path), probes(is_root=lambda: True))
    assert by_key(checks, "root").state == "warn"


# --- the whole ---------------------------------------------------------------

def test_one_area_that_fails_does_not_hide_the_others(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(sc, "check_tailscale", broken)
    result = sc.run_all(config(tmp_path), probes())
    errors = [c for c in result["checks"] if c["key"] == "error"]
    assert [c["area"] for c in errors] == ["tailscale"]
    assert {c["area"] for c in result["checks"]} == {"ports", "drives", "tailscale", "permissions"}


def test_every_sentence_is_marked_for_translation_and_translated():
    """Each template sent is a said() literal, so the locale guard and the
    Tamil file know it."""
    source = (ROOT / "ninaivu" / "server" / "setup_check.py").read_text(encoding="utf-8")
    templates = set(re.findall(r'said\("((?:[^"\\]|\\.)*)"\)', source))
    ta = json.loads((ROOT / "ninaivu" / "static" / "i18n" / "ta.json").read_text(encoding="utf-8"))
    assert templates and not sorted(t for t in templates if t not in ta)


# --- the route ---------------------------------------------------------------

def test_the_route_is_for_admins_on_the_console(as_admin, as_family, monkeypatch):
    fine = probes()
    monkeypatch.setattr(sc, "Probes", lambda: fine)
    response = as_admin.get("/api/admin/setup-check?fresh=1")
    assert response.status_code == 200
    body = response.get_json()
    assert [a["key"] for a in body["areas"]] == ["ports", "drives", "tailscale", "permissions"]
    assert set(body["counts"]) == {"ok", "warn", "fix", "info"}
    assert as_family.get("/api/admin/setup-check").status_code in (401, 403)


def test_the_page_is_in_the_console():
    page = (ROOT / "ninaivu" / "templates" / "admin.html").read_text(encoding="utf-8")
    assert 'data-tab="setup-check"' in page and 'data-panel="setup-check"' in page
    script = (ROOT / "ninaivu" / "static" / "js" / "admin.js").read_text(encoding="utf-8")
    assert "import('./pages/console/setup-check.js')" in script


@pytest.mark.parametrize("path,platform,expected", [
    ("/Volumes/J Red/Photos", "darwin", ("J Red", "/Volumes/J Red")),
    ("/Users/j/Pictures", "darwin", ("", "")),
    ("/media/demo/N300/originals", "linux", ("N300", "/media/demo/N300")),
    ("/run/media/demo/N300/x", "linux", ("N300", "/run/media/demo/N300")),
    ("/home/demo/Pictures", "linux", ("", "")),
    ("C:\\Users\\j\\Pictures", "win32", ("", "")),
])
def test_which_drive_a_folder_is_on(path, platform, expected):
    assert sc._drive_of(path, platform) == expected
