"""Two computers from the first day: the Secondary pairing with the Primary's
code, the first copy made automatically or by hand, and what is said when
the two cannot meet or the disk is too small.

A copy made by hand is only read and fingerprinted here: a file found at a
path with the content the Primary names is kept without crossing the
network, and given the Primary's modified time; anything else is sent, and
a different file at that path is set aside, never written over.
"""

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from conftest import login
import test_mugil_two_computers as two
from ninaivu.mugil import adopt, pairing
from ninaivu.mugil.courier import NOT_YET, Courier, PartnerError, _ready
from ninaivu.mugil.hold import SET_ASIDE, Holding

# The two computers, as the tests of pairing and sending set them up.
primary, secondary, two_apps = two.primary, two.secondary, two.two_apps
partner_through, run = two.partner_through, two.run


# --- the Secondary pairs with the code the Primary shows ------------------------

@pytest.fixture()
def secondary_first(two_apps, tmp_path, monkeypatch):
    """Both apps with their parts chosen; the Secondary's requests reach the
    Primary's app."""
    from conftest import ADMIN
    from ninaivu.api import mugil_api
    primary_app, secondary_app, other = two_apps
    monkeypatch.setattr(mugil_api, "Partner", partner_through(primary_app.test_client()))
    second = login(secondary_app.test_client(), *ADMIN)
    first = login(primary_app.test_client(), *ADMIN)
    folder = tmp_path / "n300"
    folder.mkdir()
    assert second.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(folder)}).status_code == 200
    assert first.post("/api/admin/mugil/role", json={"role": "primary"}).status_code == 200
    return first, second, primary_app, other


def test_the_secondary_pairs_with_the_code_the_primary_shows(secondary_first):
    first, second, primary_app, other = secondary_first
    code = first.post("/api/admin/mugil/code").get_json()["code"]
    paired = second.post("/api/admin/mugil/join", json={"url": "mac-mini", "code": code})
    assert paired.status_code == 200, paired.get_json()
    assert paired.get_json()["paired"] is True
    primary_cfg = primary_app.config["MV_CONFIG"]
    mine, theirs = pairing.load(primary_cfg.state_dir), pairing.load(other.state_dir)
    assert mine["key"] == theirs["key"]
    assert mine["role"] == "primary" and theirs["role"] == "secondary"
    # The Primary reaches the Secondary where the request came from, on the
    # port the Secondary said its family app answers.
    assert primary_cfg.mugil_partner_url == f"http://127.0.0.1:{other.port}"
    assert first.get("/api/admin/mugil").get_json()["paired"] is True
    # And the first copy is owed soon, not at tomorrow's run.
    assert primary_app.config["MV_SERVICES"].mugil_courier._first_copy_at > time.time()


def test_a_wrong_code_says_so_and_pairs_nothing(secondary_first):
    first, second, primary_app, other = secondary_first
    first.post("/api/admin/mugil/code")
    answer = second.post("/api/admin/mugil/join", json={"url": "mac-mini", "code": "AAAA-BBBB"})
    assert answer.status_code == 403
    assert "code" in answer.get_json()["error"]
    assert pairing.load(other.state_dir) is None
    assert pairing.load(primary_app.config["MV_CONFIG"].state_dir) is None


def test_a_computer_that_is_not_a_primary_says_so(secondary_first):
    first, second, _primary_app, _other = secondary_first
    first.post("/api/admin/mugil/role", json={"role": "single"})
    answer = second.post("/api/admin/mugil/join", json={"url": "mac-mini", "code": "AAAA-BBBB"})
    assert answer.status_code == 409
    assert "Primary" in answer.get_json()["error"]


def test_a_primary_that_is_off_leaves_the_secondary_waiting(secondary_first, monkeypatch):
    from ninaivu.api import mugil_api
    _first, second, _primary_app, other = secondary_first
    secondary_services = second.application.config["MV_SERVICES"]
    secondary_services.mugil_welcome.close()

    class Off(mugil_api.Partner):
        def request(self, *args, **kwargs):
            raise PartnerError("connection refused", 0)
    monkeypatch.setattr(mugil_api, "Partner", Off)
    answer = second.post("/api/admin/mugil/join", json={"url": "mac-mini", "code": "AAAA-BBBB"})
    assert answer.status_code == 502
    assert answer.get_json()["waiting"] is True
    # It waits for the Primary to find it when it is back.
    assert secondary_services.mugil_welcome.waiting
    assert pairing.load(other.state_dir) is None


def test_a_paired_primary_shows_no_code(secondary_first):
    first, second, _primary_app, _other = secondary_first
    code = first.post("/api/admin/mugil/code").get_json()["code"]
    assert second.post("/api/admin/mugil/join", json={"url": "mac-mini", "code": code}).status_code == 200
    assert first.post("/api/admin/mugil/code").status_code == 409


def test_a_secondary_without_its_folder_cannot_pair(secondary_first):
    first, second, _primary_app, other = secondary_first
    other.mugil_originals_dir = ""
    code = first.post("/api/admin/mugil/code").get_json()["code"]
    answer = second.post("/api/admin/mugil/join", json={"url": "mac-mini", "code": code})
    assert answer.status_code == 409


@pytest.mark.parametrize("typed, port, url", [
    ("mac-mini", 8080, "http://mac-mini:8080"),
    ("mac-mini", 80, "http://mac-mini"),
    ("100.64.1.2:3000", 8080, "http://100.64.1.2:3000"),
    ("http://mac.tail1234.ts.net/", 8080, "http://mac.tail1234.ts.net:8080"),
    ("https://mac.tail1234.ts.net", 8080, "https://mac.tail1234.ts.net"),
    ("", 8080, ""),
])
def test_the_primarys_address_can_be_typed_loosely(typed, port, url):
    from ninaivu.api.mugil_api import partner_address
    assert partner_address(typed, port) == url


def test_the_first_copy_starts_by_itself_soon_after_pairing(cfg, monkeypatch):
    from ninaivu.storage import db
    courier = Courier(cfg, lambda: db.connect(cfg.db_path))
    started = threading.Event()
    monkeypatch.setattr(courier, "ready", lambda: None)
    monkeypatch.setattr(courier, "start", lambda: started.set() or {"started": True})
    courier.keep()
    try:
        courier.soon(after=0.2)
        assert started.wait(5)
    finally:
        courier.stop(join=True)


# --- a copy made by hand --------------------------------------------------------

def _copy_by_hand(cfg, folder):
    """The library folder copied as a file manager would: new modified times."""
    root = Path(cfg.roots[0])
    target = folder / root.name
    shutil.copytree(root, target, ignore=shutil.ignore_patterns(".*"))
    for path in target.rglob("*"):
        if path.is_file():
            os.utime(path, (1_700_000_000, 1_700_000_000))
    return root, target


def _check(services):
    services.mugil_adopter.finished_copying()
    services.mugil_adopter._thread.join(30)
    assert adopt.state(services.cfg) == adopt.READY, services.mugil_adopter.status()


def test_the_primary_waits_while_the_copy_by_hand_is_made(primary, secondary, as_admin,
                                                          monkeypatch):
    from ninaivu.mugil import courier as courier_module
    monkeypatch.setattr(courier_module, "AWAY_FIRST", 0.05)
    _, courier = primary
    answer = as_admin.post("/api/admin/mugil/role", json={"role": "secondary",
                                                          "copy_mode": "manual"})
    assert answer.get_json()["manual"]["state"] == adopt.COPYING
    services = as_admin.application.config["MV_SERVICES"]
    # Finished a little later, and checked.
    threading.Timer(0.3, services.mugil_adopter.finished_copying).start()
    result = run(courier)
    assert result["complete"] is True, result
    with pytest.raises(PartnerError) as caught:
        _ready({"role": "secondary", "originals_ready": True, "manual": {"state": "checking"}})
    assert caught.value.status == NOT_YET


def test_have_waits_too_while_the_copy_by_hand_is_not_checked(primary, secondary, as_admin):
    mine, courier = primary
    as_admin.post("/api/admin/mugil/role", json={"role": "secondary", "copy_mode": "manual"})
    partner = courier.partner(mine.mugil_partner_url, pairing.load(mine.state_dir)["key"])
    with pytest.raises(PartnerError) as caught:
        partner.request("POST", "/api/mugil/have", body={"library": "x" * 10, "items": []})
    assert caught.value.status == NOT_YET


def test_a_copy_by_hand_is_kept_without_sending_and_gets_its_times_back(
        primary, secondary, as_admin, scanned):
    _, courier = primary
    cfg, folder = secondary
    root, target = _copy_by_hand(cfg, folder)
    as_admin.post("/api/admin/mugil/role", json={"role": "secondary", "copy_mode": "manual"})
    services = as_admin.application.config["MV_SERVICES"]
    _check(services)
    files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
    indexed = scanned[1].execute("SELECT COUNT(*) FROM assets WHERE root=? AND trashed=0",
                                 (str(root),)).fetchone()[0]
    result = run(courier)
    assert result["complete"] is True, result
    assert result["sent"] == 0 and result["bytes"] == 0, result
    assert result["already"] == indexed
    for original in files:
        rel = original.relative_to(root)
        kept = target / rel
        assert kept.read_bytes() == original.read_bytes()
        if scanned[1].execute("SELECT 1 FROM assets WHERE root=? AND rel_path=?",
                              (str(root), rel.as_posix())).fetchone():
            # The Primary's time, put back.
            assert kept.stat().st_mtime == pytest.approx(original.stat().st_mtime, abs=1e-3)
    # The next run finds every one kept.
    assert run(courier)["already"] == indexed


def test_a_file_spoiled_in_the_copy_by_hand_is_sent_and_the_bad_one_set_aside(
        primary, secondary, as_admin, scanned):
    _, courier = primary
    cfg, folder = secondary
    root, target = _copy_by_hand(cfg, folder)
    rel = scanned[1].execute("SELECT rel_path FROM assets WHERE root=? AND trashed=0 LIMIT 1",
                             (str(root),)).fetchone()["rel_path"]
    # Cut short, as a copy pulled out halfway would be.
    data = (target / rel).read_bytes()
    (target / rel).write_bytes(data[: len(data) // 2])
    missing = scanned[1].execute("SELECT rel_path FROM assets WHERE root=? AND trashed=0 "
                                 "AND rel_path != ? LIMIT 1", (str(root), rel)).fetchone()["rel_path"]
    (target / missing).unlink()
    as_admin.post("/api/admin/mugil/role", json={"role": "secondary", "copy_mode": "manual"})
    _check(as_admin.application.config["MV_SERVICES"])
    result = run(courier)
    assert result["complete"] is True, result
    assert result["sent"] == 2, result
    assert (target / rel).read_bytes() == (root / rel).read_bytes()
    assert (target / missing).read_bytes() == (root / missing).read_bytes()
    aside = [p for p in (folder / SET_ASIDE).rglob("*") if p.is_file()]
    assert [p.read_bytes() for p in aside] == [data[: len(data) // 2]]


def test_the_check_carries_on_where_it_was(secondary, as_admin, monkeypatch):
    cfg, folder = secondary
    _copy_by_hand(cfg, folder)
    as_admin.post("/api/admin/mugil/role", json={"role": "secondary", "copy_mode": "manual"})
    services = as_admin.application.config["MV_SERVICES"]
    _check(services)
    read = []
    original = adopt.Adopter._fingerprint
    monkeypatch.setattr(adopt.Adopter, "_fingerprint",
                        lambda self, path, algo, size: read.append(path) or original(
                            self, path, algo, size))
    # Asked again (more was copied, say): nothing already read is read again.
    services.mugil_adopter.copying()
    assert adopt.state(services.cfg) == adopt.COPYING
    extra = folder / Path(cfg.roots[0]).name / "later.jpg"
    extra.write_bytes(b"later")
    _check(services)
    assert read == [extra]


def test_a_check_cut_short_by_a_restart_resumes(secondary, as_admin):
    cfg, folder = secondary
    _copy_by_hand(cfg, folder)
    as_admin.post("/api/admin/mugil/role", json={"role": "secondary", "copy_mode": "manual"})
    services = as_admin.application.config["MV_SERVICES"]
    adopt._save(cfg.state_dir, {"state": adopt.CHECKING})
    fresh = adopt.Adopter(cfg)
    fresh.resume()
    fresh._thread.join(30)
    assert adopt.state(cfg) == adopt.READY
    assert Holding(folder).found_summary()["files"] > 0
    assert services.mugil_adopter is not fresh


def test_the_copy_by_hand_is_only_for_a_secondary(app, as_admin):
    assert as_admin.post("/api/admin/mugil/manual", json={"done": True}).status_code == 409


def test_automatic_needs_no_check(secondary, as_admin):
    cfg, _ = secondary
    assert adopt.state(cfg) == adopt.READY
    status = as_admin.get("/api/admin/mugil").get_json()
    assert status["copy_mode"] == "automatic"
    assert as_admin.post("/api/admin/mugil/manual", json={"done": True}).status_code == 409


# --- what the Secondary is told, and what is said ---------------------------------

def test_the_secondary_learns_the_primarys_libraries(primary, secondary, as_admin, scanned):
    _, courier = primary
    cfg, _ = secondary
    run(courier)
    status = as_admin.get("/api/admin/mugil").get_json()
    libraries = status["primary"]["libraries"]
    assert [lib["name"] for lib in libraries] == [Path(r).name for r in cfg.roots]
    assert libraries[0]["files"] > 0 and libraries[0]["bytes"] > 0
    assert status["primary"]["algo"]


def test_a_disk_too_small_is_said_before_sending(primary, secondary, monkeypatch):
    from ninaivu.mugil import hold
    mine, courier = primary
    told = []
    courier.notify = lambda event, summary, detail: told.append(event)
    monkeypatch.setattr(hold.Holding, "summary",
                        lambda self: {"folder": str(self.folder), "files": 0, "bytes": 0,
                                      "libraries": [], "free_bytes": hold.RESERVE + 10})
    result = run(courier)
    assert result["short_of_room"]["needed"] > 10
    assert "secondary_disk" in told
