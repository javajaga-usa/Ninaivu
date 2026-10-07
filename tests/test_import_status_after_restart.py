"""What an import says about itself while Ninaivu is starting again.

Restarting Ninaivu, or the computer, in the middle of a large import left a
quarter of an hour in which nothing on any screen and nothing in the server log
said what was happening. Start-up loaded the image model before it picked the
import up, and the import then walked every source to count it before copying
anything, with a total of nought until that walk ended. These hold start-up to
saying, from the first moment, that an import is about to carry on and how much
of it was done, and the count to saying how far it has got.
"""

import logging
import threading
import time

import pytest

from ninaivu import Services, _VisionOnceLoaded
from ninaivu.archive import scanner
from ninaivu.archive import database as db
from ninaivu.server import activity

from test_archive_resume import interrupted, job_row, photos, wait_for_the_job
from test_archive_resume import work as _work


@pytest.fixture()
def work(tmp_path):
    scanner.forget_resume()
    yield from _work.__wrapped__(tmp_path)
    scanner.forget_resume()


def finished(path):
    db.claim_file(str(path), path.name, 10, 1.0, job_id=1)
    db.set_status(str(path), "verified")


def test_start_up_says_an_import_is_about_to_carry_on(work):
    source, dest = photos(work / "source"), work / "dest"
    old = interrupted(source, dest)
    db.update_job(old, total_files=6)
    for path in sorted((source / "album").iterdir())[:4]:
        finished(path)

    note = scanner.announce_resume()

    assert note["job_id"] == old
    progress = scanner.job_progress()
    assert progress["stage"] == "resuming"
    assert progress["after_restart"]["finished"] == 4
    assert progress["after_restart"]["total_files"] == 6
    assert not scanner.is_scanning(), "said, not started"


def test_the_activity_strip_names_it_before_it_starts(work):
    source, dest = photos(work / "source"), work / "dest"
    old = interrupted(source, dest)
    db.update_job(old, total_files=6)
    scanner.announce_resume()

    job = activity._archive(None, None)

    assert job["title"] == "Import — resuming after restart"
    assert job["detail"].startswith("0 of 6 done before the restart")
    assert job["paused"] is True


def test_nothing_is_announced_without_an_interrupted_import(work):
    assert scanner.announce_resume() is None
    assert scanner.job_progress()["stage"] is None
    assert activity._archive(None, None) is None


def test_picking_it_up_replaces_the_note_with_the_running_job(work):
    source, dest = photos(work / "source"), work / "dest"
    old = interrupted(source, dest)
    scanner.announce_resume()

    job, problems = scanner.resume_interrupted()

    assert (job["id"], problems) == (old, [])
    assert scanner.resume_pending() is None
    assert scanner._job.after_restart["job_id"] == old
    wait_for_the_job()


def test_a_missing_folder_is_given_as_the_reason_it_waits(work):
    source, dest = work / "share", work / "dest"
    interrupted(source, dest)
    scanner.announce_resume()

    job, problems = scanner.resume_interrupted()

    assert job is not None and problems
    assert scanner.job_progress()["after_restart"]["waiting"]


def test_stop_while_it_waits_means_it_is_not_picked_up_again(work):
    source, dest = work / "share", work / "dest"
    old = interrupted(source, dest)
    scanner.announce_resume()

    scanner.stop_scan()

    assert scanner.resume_pending() is None
    assert job_row(old)["state"] == "stopped"
    assert db.interrupted_job() is None


def test_start_up_stops_retrying_once_stop_is_pressed(work, cfg, monkeypatch):
    services = Services(cfg)
    source, dest = work / "share", work / "dest"
    interrupted(source, dest)
    monkeypatch.setattr(Services, "ARCHIVE_RESUME_WAIT", 0.05)
    monkeypatch.setattr("ninaivu.api.archive_api._yield_the_disk",
                        lambda scanner_, label, power=None: None)

    services._resume_archive()
    scanner.stop_scan()
    photos(source)
    time.sleep(0.5)

    assert scanner._job is None, "carried on although Stop was pressed"


def test_the_count_before_copying_says_how_far_it_has_got(work, caplog):
    source, dest = photos(work / "source"), work / "dest"
    job = scanner.ArchiveJob([str(source)], str(dest))
    seen = []
    walked = job._walk

    def watching():
        for found in walked():
            seen.append((job.stage, job.counted))
            yield found

    job._walk = watching
    job.job_id = None
    with caplog.at_level(logging.INFO, logger="ninaivu.archive.scanner"):
        assert job.preflight() == 6

    assert seen[0] == ("counting", 0)
    assert seen[-1] == ("counting", 5)
    assert job.counted == 6
    said = caplog.text
    assert "counting the files" in said and "counted 6 files" in said


def test_the_model_is_waited_for_and_its_absence_is_no_opinion():
    ready = threading.Event()
    assert not _VisionOnceLoaded(None, ready).unavailable, "unknown until loaded"

    class Engine:
        model_id = "clip"

        def embed_images(self, images):
            return ["image"] * len(images)

        def embed_texts(self, texts):
            return ["text"] * len(texts)

    class Ai:
        engine = None

        def get_engine(self):
            return self.engine

    services = Services.__new__(Services)
    services._ai_mod = Ai()
    vision = _VisionOnceLoaded(services, ready)

    answer = []
    asking = threading.Thread(target=lambda: answer.append(vision.embed_images([1, 2])))
    asking.start()
    time.sleep(0.1)
    assert not answer, "asked the model before it had loaded"
    services._ai_mod.engine = Engine()
    ready.set()
    asking.join(timeout=5)
    assert answer == [["image", "image"]]
    assert vision.model_id == "clip"

    assert not vision.unavailable
    services._ai_mod.engine = object()
    assert vision.unavailable, "no model: the import does not ask"
    with pytest.raises(RuntimeError):
        vision.embed_texts(["a"])
