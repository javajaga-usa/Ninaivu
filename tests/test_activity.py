"""The status strip tells the truth about what is using the machine.

It used to show the library scan and nothing else, so a consolidation, a cloud
upload, a straightening pass or a storage check could hold the disk for hours
with nothing anywhere on screen to say so. These tests are about that: every
running job turns up, each one says what it is spending, and none of them
leaks to a household member who is not an administrator.
"""

from __future__ import annotations

import pytest

from ninaivu.server import activity


@pytest.fixture()
def services(app):
    return app.config["MV_SERVICES"]


@pytest.fixture()
def scanner(app):
    return app.config["MV_SCANNER"]


def by_id(jobs):
    return {job["id"]: job for job in jobs}


# -- nothing running --------------------------------------------------------

def test_an_idle_machine_says_nothing(services):
    assert activity.running(services) == []


def test_the_endpoint_says_nothing_is_running(as_admin):
    body = as_admin.get("/api/status/activity").get_json()
    assert body["jobs"] == []
    assert body["running"] is False
    assert body["uses"] == []


# -- indexing ---------------------------------------------------------------

def test_indexing_appears_with_its_phase_and_its_folder(services, scanner):
    scanner.progress.update(status="indexing", total=100, processed=40,
                            folder="2019/Cornwall", started_at=1.0, ended_at=0.0)
    job = by_id(activity.running(services))["indexing"]
    assert job["title"] == "Indexing"
    assert "40 of 100" in job["detail"]
    assert "2019/Cornwall" in job["detail"]
    assert job["percent"] == 40
    assert job["uses"] == ["disk"]


def test_a_walk_shows_no_percentage_it_cannot_stand_behind(services, scanner):
    """The count it is heading for is the thing it is still finding."""
    scanner.progress.update(status="walking", total=0, processed=12,
                            started_at=1.0, ended_at=0.0)
    assert by_id(activity.running(services))["indexing"]["percent"] is None


def test_an_ai_phase_says_it_is_spending_the_processor(services, scanner):
    """A phase that is slow on a fast disk is a different question."""
    scanner.progress.update(status="tagging", tagged=3, tag_total=12,
                            started_at=1.0, ended_at=0.0)
    job = by_id(activity.running(services))["indexing"]
    assert job["title"] == "Analysing with AI"
    assert job["uses"] == ["cpu", "disk"]
    assert job["detail"] == "3 of 12"


def test_a_paused_scan_names_what_it_is_waiting_for(services, scanner):
    """"Paused" on its own reads as something gone wrong. It is not."""
    scanner.defer("checking the library's storage")
    try:
        scanner.progress.update(status="paused", total=100, processed=40,
                                started_at=1.0, ended_at=0.0)
        job = by_id(activity.running(services))["indexing"]
        assert job["paused"] is True
        assert job["detail"] == ("Waiting for checking the library's storage "
                                 "to finish")
        # Nothing is moving, so there is no percentage and no ETA to give.
        assert job["percent"] is None
    finally:
        scanner.resume("checking the library's storage")


def test_a_finished_scan_leaves_the_strip(services, scanner):
    scanner.progress.update(status="done", total=100, processed=100,
                            started_at=1.0, ended_at=2.0)
    assert activity.running(services) == []


# -- the jobs that used to be invisible -------------------------------------

def test_a_storage_check_is_visible_while_it_holds_the_disk(services,
                                                            monkeypatch):
    from ninaivu.api import admin_api

    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "running", True)
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "total", 5000)
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "processed", 1250)
    job = by_id(activity.running(services))["storage-check"]
    assert job["title"] == "Checking the library's storage"
    assert job["detail"] == "1,250 of 5,000"
    assert job["percent"] == 25
    assert job["page"] == "activity"


def test_a_model_download_is_visible(services, monkeypatch):
    from ninaivu.media import model_catalog

    monkeypatch.setattr(model_catalog.downloads, "_state", {
        "siglip2": {"status": "downloading", "done_bytes": 100 * 1048576,
                    "total_bytes": 400 * 1048576},
        "lama": {"status": "installed"},
    })
    jobs = by_id(activity.running(services))
    assert "model:lama" not in jobs            # finished is not running
    job = jobs["model:siglip2"]
    assert job["percent"] == 25
    assert job["uses"] == ["network"]


def test_an_install_shows_the_installers_own_last_word(services, monkeypatch):
    """There is no total to measure against, so it does not invent a bar."""
    from ninaivu.media import components

    monkeypatch.setattr(components.installs, "_state", {
        "ffmpeg": {"status": "installing",
                   "log": ["winget install ffmpeg", "Downloading 40%"]},
    })
    job = by_id(activity.running(services))["install:ffmpeg"]
    assert job["detail"] == "Downloading 40%"
    assert job["percent"] is None


def test_converting_a_video_for_the_browser_is_visible(services, app):
    """Two ffmpeg runs at once is the heaviest thing Ninaivu does.

    It used to be visible only to whoever opened the video, so a clip
    somebody started on their phone and gave up on went on transcoding with
    nothing anywhere to say why the machine was busy.
    """
    from ninaivu.utils import proxies

    store = proxies.ProxyStore(services.cfg.state_dir)
    store._builds = {
        7: proxies.BuildState(7, "building", 40, "Converting…"),
        9: proxies.BuildState(9, "ready", 100),
    }
    app.config["MV_PROXIES"] = store
    job = by_id(activity.running(services, app.config))["conversions"]
    assert job["uses"] == ["cpu", "disk"]
    assert job["percent"] == 40                 # the only one running

    store._builds[9] = proxies.BuildState(9, "building", 5)
    job = by_id(activity.running(services, app.config))["conversions"]
    # Which video is being converted is not the question, and two rows saying
    # "Converting" is noise.
    assert job["detail"] == "2 videos"
    assert job["percent"] is None


def test_nothing_is_reported_before_a_video_has_ever_been_converted(services,
                                                                    app):
    """The store is built on first use, so it may simply not exist yet."""
    app.config.pop("MV_PROXIES", None)
    assert "conversions" not in by_id(activity.running(services, app.config))


def test_the_strip_names_a_drive_the_cloud_upload_cannot_reach(services):
    """An upload sending nothing must not look like one that is working."""
    from ninaivu.cloud import engine as engine_mod

    class Stub:
        running = True

        def __init__(self):
            self.state = engine_mod.SyncState()

    root = r"E:\MasterArchive"
    stub = Stub()
    stub.state.update(offline_roots=[root], current="IMG_4410.HEIC",
                      current_sent=10, current_total=100)
    services.cloud._engine = stub
    try:
        job = by_id(activity.running(services))["cloud"]
        assert job["detail"] == f"Waiting for {root} — not connected"
        assert job["paused"] is True
        # Nothing is moving, so there is no percentage and no ETA to offer.
        assert job["percent"] is None
        assert job["eta"] is None
    finally:
        services.cloud._engine = None


def test_a_backup_being_made_is_visible(services):
    """Bundling copies every database and restores the copy to check it."""
    keeper = services.backups
    assert keeper.busy is False
    assert keeper._running.acquire(blocking=False)
    try:
        assert keeper.busy is True
        assert by_id(activity.running(services))["backup"]["uses"] == ["disk"]
    finally:
        keeper._running.release()


# -- more than one at once --------------------------------------------------

def test_several_jobs_are_listed_heaviest_first(services, scanner, monkeypatch):
    """The line most likely to be the answer is the one nearest the top."""
    from ninaivu.api import admin_api

    scanner.progress.update(status="indexing", total=10, processed=5,
                            started_at=1.0, ended_at=0.0)
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "running", True)
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "total", 10)
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "processed", 1)
    assert [job["id"] for job in activity.running(services)] == [
        "indexing", "storage-check"]


def test_a_subsystem_that_cannot_answer_costs_only_its_own_line(
        services, scanner, monkeypatch):
    """Half a strip beats a page with an exception behind it."""
    def broken(_services):
        raise RuntimeError("the drive stopped answering")

    scanner.progress.update(status="indexing", total=10, processed=5,
                            started_at=1.0, ended_at=0.0)
    monkeypatch.setattr(activity, "SOURCES", [broken, activity._indexing])
    assert [job["id"] for job in activity.running(services)] == ["indexing"]


# -- who gets to see it -----------------------------------------------------

def test_the_endpoint_carries_the_scan_so_the_gallery_asks_once(as_admin,
                                                                scanner):
    """The gallery still keys its refresh and its toast off the scan block."""
    scanner.progress.update(status="indexing", total=100, processed=40,
                            added=7, removed=1, started_at=1.0, ended_at=0.0)
    body = as_admin.get("/api/status/activity").get_json()
    assert body["scan"]["added"] == 7
    assert body["scan"]["running"] is True
    assert body["running"] is True
    assert body["uses"] == ["disk"]


@pytest.mark.parametrize("who", ["as_family", "as_guest", "anon"])
def test_nobody_else_learns_what_the_machine_is_doing(who, request, scanner):
    """Which drives are being read is not the family's business.

    The same rule /api/status/scan applies: a household member sees their
    photographs, not the folder names of a consolidation or the account a
    backup is going to.
    """
    scanner.progress.update(status="indexing", total=10, processed=4,
                            root="/secret/place", folder="secret/folder",
                            started_at=1.0, ended_at=0.0)
    response = request.getfixturevalue(who).get("/api/status/activity")
    if response.status_code != 200:
        return
    body = response.get_json()
    assert body["jobs"] == []
    assert body["running"] is False
    assert "secret" not in repr(body)


# -- how far through the library, not how far through the file ---------------

def test_the_cloud_bar_is_the_library_not_the_file_in_flight(services,
                                                             monkeypatch):
    """It drew `current_sent / current_total` — the bytes of the file on the
    wire. So it filled and emptied every few seconds, and read 90% with a
    hundred thousand photographs still waiting. What somebody wants from that
    line is how much of their library is safe.
    """
    from ninaivu.cloud import engine as engine_mod

    class Stub:
        running = True

        def __init__(self):
            self.state = engine_mod.SyncState()

    stub = Stub()
    # The file in flight is nearly done; the library is nowhere near.
    stub.state.update(current="IMG_0757.JPG", current_sent=95, current_total=100)
    services.cloud._engine = stub
    monkeypatch.setattr(services.cloud, "queue_progress", lambda: (34_167, 156_053))
    try:
        job = by_id(activity.running(services))["cloud"]
        assert job["percent"] == 21, job          # 34,167 of 156,053, not 95%
        assert "34,167 of 156,053" in job["detail"], job["detail"]
        assert "IMG_0757.JPG" in job["detail"], job["detail"]
    finally:
        services.cloud._engine = None


def test_it_still_says_something_when_the_queue_cannot_be_counted(services,
                                                                  monkeypatch):
    """A reading nobody can take is not worth losing the whole line over."""
    from ninaivu.cloud import engine as engine_mod

    class Stub:
        running = True

        def __init__(self):
            self.state = engine_mod.SyncState()

    stub = Stub()
    stub.state.update(current="IMG_0757.JPG", current_sent=95, current_total=100)
    services.cloud._engine = stub

    def broken():
        raise RuntimeError("the index is busy")

    monkeypatch.setattr(services.cloud, "queue_progress", broken)
    try:
        job = by_id(activity.running(services))["cloud"]
        assert job["title"] == "Backing up to the cloud"
        assert job["detail"].startswith("IMG_0757.JPG")
    finally:
        services.cloud._engine = None


def test_an_empty_queue_is_not_reported_as_a_percentage(services, monkeypatch):
    from ninaivu.cloud import engine as engine_mod

    class Stub:
        running = True

        def __init__(self):
            self.state = engine_mod.SyncState()

    stub = Stub()
    stub.state.update(current="IMG_0757.JPG", current_sent=0, current_total=0)
    services.cloud._engine = stub
    monkeypatch.setattr(services.cloud, "queue_progress", lambda: (0, 0))
    try:
        assert by_id(activity.running(services))["cloud"]["percent"] is None
    finally:
        services.cloud._engine = None
