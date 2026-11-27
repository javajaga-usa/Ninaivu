"""Mugil says how its copies stand, in words a person can act on.

Found in real use (27 November 2026): the second copy and the originals sent
to the Secondary said what they were doing as one paragraph, and some of what
they knew never reached the page at all.

* The last run of the originals said "0 sent, 0 already there" after every
  run that finished: its counts were copied before the run made them.
* The second copy worked out what a copy or a check came to, and the page
  never showed it; during a check it still said Copying, with no count.
* Neither said when it would run again by itself.
* The update of the Secondary showed nothing between "sent" and "running",
  just when the Secondary is away switching.
* Folders → Second copy drew bare folder names where the library draws a
  picture, a count and a size.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.mugil.courier import Courier
from ninaivu.server import auth
from ninaivu.storage import db, mirror_place

ROOT = Path(__file__).resolve().parents[1]


# -- the originals sent to the Secondary ---------------------------------------

def test_the_last_run_keeps_what_it_counted(cfg, monkeypatch):
    courier = Courier(cfg, lambda: db.connect(cfg.db_path))

    def run(totals):
        totals.update(sent=3, bytes=3000, already=40)
        return {"complete": True, "stopped": False}
    monkeypatch.setattr(courier, "run", run)
    courier._run_logged()                                           # noqa: SLF001
    assert (courier.last["sent"], courier.last["bytes"], courier.last["already"]) == (3, 3000, 40)
    assert courier.last["complete"] is True and courier.last["finished"] > 0


def test_the_next_run_is_said(cfg):
    courier = Courier(cfg, lambda: db.connect(cfg.db_path))
    cfg.mugil_role, cfg.mugil_copy_every_hours = "primary", 24
    assert courier.next_at() == 0.0                    # never ran: the next look starts it
    courier.last = {"finished": 1000.0, "complete": True}
    assert courier.next_at() == 1000.0 + 24 * 3600
    courier.last = {"finished": 1000.0, "complete": False}
    assert courier.next_at() == 1000.0 + 3600          # cut short: tried again within the hour
    assert courier.status()["next_at"] == courier.next_at()
    cfg.mugil_copy_every_hours = 0
    assert courier.next_at() == 0.0                    # only when asked


# -- the second copy -----------------------------------------------------------

@pytest.fixture()
def house(scanned, tmp_path: Path):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    disk = tmp_path / "other-disk"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    client = login(create_admin_app(services).test_client(), *ADMIN)
    return {"cfg": cfg, "conn": conn, "mirror": services.mirror, "client": client}


def _wait(mirror):
    mirror.join(30)
    return mirror.status()


def test_a_copy_says_what_it_came_to(house):
    mirror = house["mirror"]
    assert mirror.start() == {"started": True}
    status = _wait(mirror)
    last = status["last"]
    assert last["job"] == "copying" and last["outcome"] == "done"
    assert last["copied"] == status["files"] > 0 and last["failed"] == 0
    assert last["finished"] > 0
    # Copied every day by default: the next one a day after this one.
    assert status["next_at"] == pytest.approx(status["last_run"] + 24 * 3600)


def test_a_check_counts_as_it_goes_and_says_what_it_found(house):
    mirror = house["mirror"]
    mirror.start()
    files = _wait(mirror)["files"]
    assert mirror.verify() == {"started": True}
    status = _wait(mirror)
    assert status["checked"] == status["to_check"] == files
    assert status["last"] == {**status["last"], "job": "checking", "outcome": "done",
                              "checked": files, "bad": 0, "missing": 0}


def test_a_check_with_nothing_copied_says_so(house):
    mirror = house["mirror"]
    mirror.verify()
    assert _wait(mirror)["last"]["outcome"] == "nothing"


def test_only_when_asked_has_no_next_copy(house):
    house["cfg"].mirror_every_hours = 0
    assert house["mirror"].status()["next_at"] == 0


def test_the_copys_folders_are_drawn_like_the_librarys(house):
    mirror = house["mirror"]
    mirror.start()
    status = _wait(mirror)
    top = house["client"].get("/api/mirror/browse").get_json()
    assert top["dirs"] and sum(d["n"] for d in top["dirs"]) + len(top["files"]) == status["files"]
    assert all(d["bytes"] > 0 for d in top["dirs"] if d["n"])
    assert any(d["cover"] for d in top["dirs"]), "a folder with pictures has one on its card"
    pictures = {r["id"] for r in house["conn"].execute("SELECT id FROM assets WHERE kind='picture'")}
    assert {d["cover"] for d in top["dirs"] if d["cover"]} <= pictures | {
        r["id"] for r in house["conn"].execute("SELECT id FROM assets WHERE kind='video'")}
    # A file copied from the library is shown with the library's own thumbnail.
    folder = next(d for d in top["dirs"] if d["n"])
    inside = house["client"].get(f"/api/mirror/browse?path={folder['path']}").get_json()
    while not inside["files"] and inside["dirs"]:
        inside = house["client"].get(
            f"/api/mirror/browse?path={inside['dirs'][0]['path']}").get_json()
    copied = [f for f in inside["files"] if f["is"] == "copied"]
    assert copied and all(isinstance(f["id"], int) and f["kind"] for f in copied)


def test_a_folder_is_counted_from_the_record_not_by_walking_it(house, monkeypatch):
    mirror = house["mirror"]
    mirror.start()
    _wait(mirror)
    walked = []
    real = mirror_place.os.scandir
    monkeypatch.setattr(mirror_place.os, "scandir", lambda p: walked.append(p) or real(p))
    mirror_place.listing(mirror, house["conn"], "")
    assert len(walked) == 1, "only the folder shown is read from the disk"


# -- the page ------------------------------------------------------------------

def test_no_two_elements_of_the_console_share_an_id():
    """The setup check and the second copy were both #sc-block, so a copy
    under way disabled the setup check's Check again too."""
    page = (ROOT / "ninaivu/templates/admin.html").read_text(encoding="utf-8")
    ids = Counter(re.findall(r'\sid="([^"{}]+)"', page))
    assert not [i for i, n in ids.items() if n > 1]


def test_each_status_is_one_card_not_one_paragraph():
    js = ROOT / "ninaivu/static/js"
    second = (js / "second-copy.js").read_text(encoding="utf-8")
    two = (js / "two-computers.js").read_text(encoding="utf-8")
    assert "renderStatus($('#sc-status')" in second and "#sc-progress" not in second
    assert "renderStatus($('#tc-status')" in two and "#tc-progress" not in two
    assert "renderStatus($('#tc-update-status')" in two
    # A check is called a check, and what the last run came to is said.
    assert "i18n.t('Checking')" in second and "lastRun(status.last)" in second
    # An update under way is asked about every few seconds.
    assert "Boolean(this.updateBusy)" in two
