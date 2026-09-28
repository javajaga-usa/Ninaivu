"""Course material stays out of the family archive until somebody says otherwise.

A drive kept for years collects downloaded courses beside the photographs:
folders of numbered lesson videos tagged by the site they came from. These run
real archive jobs over a tree shaped like one of those drives, and check the
promises that make holding a folder back safe: nothing that looks like a course
is copied until someone decides; a real camera's photographs are never held
back on a guess; a decision is remembered, including when the drive comes back
under another letter; and the estimate, the dry run and the real run all agree.
"""
import io
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ninaivu import archive                                      # noqa: E402
from ninaivu.archive import course_material, scanner, status_kit          # noqa: E402
from ninaivu.archive import database as db                       # noqa: E402
from ninaivu.archive.scanner import ArchiveJob                   # noqa: E402


def video():
    """A distinct lesson: identical bytes would be archived once, as duplicates."""
    return b"\x00\x00\x00\x18ftypmp42" + os.urandom(80_000)


@pytest.fixture()
def drive(tmp_path):
    archive.configure(tmp_path)
    db.close_db()
    db.init_db()
    root = tmp_path / "E"
    root.mkdir()
    try:
        yield root, tmp_path / "Master"
    finally:
        db.close_db()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def camera_jpeg(make=True):
    """A real JPEG, big enough to archive, with or without a camera's EXIF."""
    image = Image.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3))
    exif = Image.Exif()
    exif[0x9003] = "2019:06:07 12:30:00"
    if make:
        exif[271] = "Canon"
        exif[272] = "Canon EOS 80D"
    stream = io.BytesIO()
    image.save(stream, "JPEG", quality=95, exif=exif)
    return stream.getvalue()


def a_course(parent, name, lessons=6):
    folder = parent / name
    for i in range(1, lessons + 1):
        write(folder / f"{i:02d} Lesson {i}" / f"{i}. Lecture.mp4", video())
    write(folder / "[FreeTutorials.Us].txt", b"Visit us")
    write(folder / "Freetutorials.Us.url", b"[InternetShortcut]\nURL=https://example.invalid\n")
    return folder


def a_trip(parent):
    folder = parent / "2019 Europe trip"
    for i, city in enumerate(["London", "Paris", "Rome", "Berlin"], 1):
        write(folder / f"{i:02d} {city}" / f"IMG_{i}.JPG", camera_jpeg())
    return folder


def archived_names(dest):
    return sorted(name for _root, _dirs, names in os.walk(dest) for name in names
                  if not name.startswith(".") and name != scanner.ARCHIVE_MARKER
                  and name not in status_kit.KIT_FILES)


def states(job):
    return {os.path.basename(c["path"]): c["state"] for c in job.course_folders}


# ---------------------------------------------------------------------------
# Recognising a course from its name and shape
# ---------------------------------------------------------------------------

LESSONS = [f"{i:02d} Topic {i}" for i in range(1, 12)]


@pytest.mark.parametrize("name,subdirs,files", [
    ("[FreeTutorials.Us] Udemy - javawebservicespart2", LESSONS,
     ["[FreeTutorials.Us].txt", "Freetutorials.Us.url"]),
    ("[FTUForum.com] Udemy - Ionic 4 - Build iOS, Android & Web Apps", LESSONS, []),
    ("[udemy] Java SE 8 New Features [FreeOnlineMovies.Co]", [], []),
    ("Data Structures - Abstraction and Design Using Java - 3E (2016) (Pdf) Gooner", [], []),
    ("JavaScript_Essential_Training", LESSONS, []),
    ("become-a-product-manager", LESSONS, []),
    ("Build a Real Time web app in node.js , Angular.js, mongoDB", LESSONS, []),
])
def test_the_courses_on_a_real_drive_are_recognised(name, subdirs, files):
    judged = course_material.assess(name, subdirs, files)
    assert judged.is_course, (judged.score, judged.reasons)
    assert judged.reasons


@pytest.mark.parametrize("name,subdirs,files", [
    ("2019 Europe trip", ["01 London", "02 Paris", "03 Rome", "04 Berlin"], []),
    ("2019", ["01 January", "02 February", "03 March", "04 April"], []),
    ("Java trip 2015", ["01 Jakarta", "02 Bandung", "03 Yogyakarta"], []),
    ("Gooner match day 2019", [], ["IMG_1.JPG"]),
    ("DCIM", ["100APPLE", "101APPLE"], []),
    ("Kids school play", [], ["clip1.mp4", "clip2.mp4", "clip2.srt"]),
])
def test_family_folders_are_not_mistaken_for_courses(name, subdirs, files):
    judged = course_material.assess(name, subdirs, files)
    assert not judged.is_course, (judged.score, judged.reasons)


def test_a_borderline_folder_is_tipped_by_the_courses_beside_it():
    among_courses = course_material.course_neighbourhood([
        "[FreeTutorials.Us] spring-hibernate-tutorial", "[FreeTutorials.Us] Udemy - rest-api",
        "[FTUForum.com] Udemy - Ionic 4", "Agile project management"])
    among_trips = course_material.course_neighbourhood(
        ["2015 Bali", "Java trip 2015", "2017 Japan", "Grandma 80th"])
    name, lessons = "Agile project management", LESSONS
    assert not course_material.assess(name, lessons, [], among_trips).is_course
    assert course_material.assess(name, lessons, [], among_courses).is_course
    # Context tips a borderline folder; it never makes one out of nothing.
    assert not course_material.assess("Screenshots", [], ["a.png"], among_courses).is_course


def test_a_decision_follows_the_drive_not_its_letter():
    volume = ("windows-volume", 3405691582)
    assert (course_material.decision_key(r"E:\Materials\[FreeTutorials.Us] rest-api", volume)
            == course_material.decision_key(r"F:\Materials\[FreeTutorials.Us] rest-api", volume))
    assert (course_material.decision_key(r"E:\Materials\A", volume)
            != course_material.decision_key(r"E:\Materials\A", ("windows-volume", 1)))


# ---------------------------------------------------------------------------
# What a run does with them
# ---------------------------------------------------------------------------

def test_an_undecided_course_is_held_back_and_the_photographs_still_arrive(drive):
    root, dest = drive
    a_course(root / "Materials", "[FreeTutorials.Us] Udemy - rest-api")
    a_trip(root / "Photos")

    job = ArchiveJob([str(root)], str(dest))
    job.run()

    names = archived_names(dest)
    assert names and all(n.upper().startswith("IMG_") for n in names), names
    assert not any(n.endswith(".mp4") for n in names), "no lesson was copied"
    assert states(job) == {"[FreeTutorials.Us] Udemy - rest-api": "held"}
    held = job.course_folders[0]
    assert any("course site" in r for r in held["reasons"]), held["reasons"]
    message = db.get_job(job.job_id)["message"]
    assert "held back until you decide" in message, message


def test_the_estimate_the_dry_run_and_the_real_run_agree(drive):
    root, dest = drive
    a_course(root / "Materials", "[FreeTutorials.Us] Udemy - rest-api")
    a_course(root / "Materials", "JavaScript_Essential_Training")
    a_trip(root / "Photos")

    estimate = ArchiveJob([str(root)], str(dest))
    estimate.measure_course_folders = True
    counted = [name for _root, name in estimate._walk()]

    plan = ArchiveJob([str(root)], str(dest), mode=scanner.MODE_DRY_RUN)
    plan.run()
    real = ArchiveJob([str(root)], str(dest))
    real.run()

    assert states(estimate) == states(plan) == states(real)
    assert not any(n.endswith(".mp4") for n in counted)
    assert sorted(n.upper() for n in counted) == sorted(archived_names(dest))
    for folder in estimate.course_folders:
        assert folder["files"] == 8 and folder["bytes"] > 6 * 80_000


def test_excluding_is_remembered_and_keeping_copies_it_whole(drive):
    root, dest = drive
    rest = a_course(root / "Materials", "[FreeTutorials.Us] Udemy - rest-api")
    spring = a_course(root / "Materials", "[FreeTutorials.Us] spring-hibernate-tutorial")

    first = ArchiveJob([str(root)], str(dest))
    first._walk_list = list(first._walk())
    keys = {os.path.basename(c["path"]): c["key"] for c in first.course_folders}
    db.set_folder_decision(keys[rest.name], str(rest), "exclude")
    db.set_folder_decision(keys[spring.name], str(spring), "include")

    job = ArchiveJob([str(root)], str(dest))
    job.run()

    assert states(job) == {rest.name: "excluded", spring.name: "kept"}
    names = archived_names(dest)
    assert sum(1 for n in names if n.endswith(".mp4")) == 6, names   # spring's six lessons
    # A kept course is taken whole: its lesson folders are not judged again.
    assert len(job.course_folders) == 2

    db.set_folder_decision(keys[spring.name], str(spring), None)
    again = ArchiveJob([str(root)], str(dest))
    list(again._walk())
    assert states(again)[spring.name] == "held", "forgetting a decision asks again"


def test_a_real_cameras_photographs_are_never_held_back_on_a_guess(drive):
    root, dest = drive
    course = a_course(root / "Materials", "[FreeTutorials.Us] ultimate-photoshop-training")
    write(course / "07 Lesson 7" / "my_practice_shot.jpg", camera_jpeg(make=True))

    job = ArchiveJob([str(root)], str(dest))
    job.run()

    assert states(job) == {course.name: "review"}
    assert job.course_folders[0]["camera_photos"] == 1
    names = archived_names(dest)
    assert any(n.endswith(".jpg") for n in names), "the photograph was copied"
    assert "worth a look" in db.get_job(job.job_id)["message"]


def test_a_folder_chosen_as_the_source_is_taken_as_asked(drive):
    root, dest = drive
    course = a_course(root, "[FreeTutorials.Us] Udemy - rest-api")

    job = ArchiveJob([str(course)], str(dest))
    job.run()

    assert job.course_folders == []
    assert sum(1 for n in archived_names(dest) if n.endswith(".mp4")) == 6


# ---------------------------------------------------------------------------
# Reviewing them in the console
# ---------------------------------------------------------------------------

from test_archive_console import console as console          # noqa: E402,F401


def test_the_estimate_lists_course_folders_and_a_decision_changes_it(console, tmp_path):
    client, _, _ = console
    root = tmp_path / "Drive"
    course = a_course(root / "Materials", "[FreeTutorials.Us] Udemy - rest-api")
    a_trip(root / "Photos")
    job = {"source_dirs": [str(root)], "destination_dir": str(tmp_path / "Master")}

    first = client.post("/api/archive/capacity", json=job).get_json()
    assert first["ok"], first
    [held] = first["course_folders"]
    assert held["state"] == "held" and held["files"] == 8 and held["bytes"] > 6 * 80_000
    assert held["reasons"] and "key" not in held, "the decision key never leaves the server"
    assert first["files"] == 4, "only the trip's photographs are counted"

    saved = client.post("/api/archive/course-folders/decision",
                        json={"decisions": [{"path": held["path"], "decision": "include"}]})
    assert saved.status_code == 200 and saved.get_json()["include"] == 1
    kept = client.post("/api/archive/capacity", json=job).get_json()
    assert kept["course_folders"][0]["state"] == "kept"
    assert kept["files"] == 4 + 6, "a kept course is counted like anything else"

    client.post("/api/archive/course-folders/decision",
                json={"decisions": [{"path": str(course), "decision": "exclude"}]})
    excluded = client.post("/api/archive/capacity", json=job).get_json()
    assert excluded["course_folders"][0]["state"] == "excluded" and excluded["files"] == 4

    client.post("/api/archive/course-folders/decision",
                json={"decisions": [{"path": str(course), "decision": None}]})
    again = client.post("/api/archive/capacity", json=job).get_json()
    assert again["course_folders"][0]["state"] == "held"


@pytest.mark.parametrize("body", [
    {},
    {"decisions": []},
    {"decisions": "exclude everything"},
    {"decisions": [{"path": "", "decision": "exclude"}]},
    {"decisions": [{"path": "C:/definitely/not/here", "decision": "exclude"}]},
    {"decisions": [{"path": ".", "decision": "delete"}]},
])
def test_a_malformed_decision_is_refused(console, body):
    client, _, _ = console
    response = client.post("/api/archive/course-folders/decision", json=body)
    assert response.status_code == 400, response.get_json()


# ---------------------------------------------------------------------------
# A second opinion from the pictures, for folders the names cannot settle
# ---------------------------------------------------------------------------

class PictureJudge:
    """Stands in for the image model: a dark still reads as a screen recording.

    Phrases come back as two directions -- teaching first, family second -- so
    the comparison in picture_opinion is exercised for real.
    """
    model_id = "fake-judge"

    def __init__(self):
        self.images_seen = 0

    def embed_texts(self, texts):
        import numpy as np
        half = len(course_material.TEACHING_LOOKS)
        return np.array([[1.0, 0.0]] * half + [[0.0, 1.0]] * (len(texts) - half), dtype="float32")

    def embed_images(self, images):
        import numpy as np
        self.images_seen += len(images)
        rows = []
        for image in images:
            dark = sum(image.convert("L").resize((1, 1)).getdata()) < 128
            rows.append([1.0, 0.0] if dark else [0.0, 1.0])
        return np.array(rows, dtype="float32")


def a_borderline_folder(parent, name="Agile project management", dark=True, lessons=4):
    """Numbered lessons and a subject in the name: three points, one short."""
    folder = parent / name
    shade = (25, 25, 25) if dark else (235, 220, 190)
    for i in range(1, lessons + 1):
        lesson = folder / f"{i:02d} Topic {i}"
        write(lesson / f"{i}. Lecture.mp4", video())
        stream = io.BytesIO()
        Image.new("RGB", (64, 48), shade).save(stream, "PNG")
        write(lesson / f"slide_{i}.png", stream.getvalue())
    return folder


def test_the_names_leave_it_one_short(drive):
    root, _ = drive
    folder = a_borderline_folder(root)
    judged = course_material.assess(folder.name, sorted(p.name for p in folder.iterdir()), [])
    assert judged.score == 3 and not judged.is_course, judged.reasons


def test_pictures_that_look_like_teaching_tip_a_borderline_folder(drive):
    root, dest = drive
    a_borderline_folder(root / "Downloads")
    judge = PictureJudge()
    job = ArchiveJob([str(root)], str(dest), vision=judge)
    list(job._walk())
    assert states(job) == {"Agile project management": "held"}
    assert any("look like screen recordings or slides" in r for r in job.course_folders[0]["reasons"])
    assert judge.images_seen >= 2


def test_pictures_that_look_like_family_life_change_nothing(drive):
    root, dest = drive
    a_borderline_folder(root / "Downloads", dark=False)
    job = ArchiveJob([str(root)], str(dest), vision=PictureJudge())
    list(job._walk())
    assert job.course_folders == []


def test_the_opinion_is_kept_so_a_run_without_the_model_agrees(drive):
    """The estimate may have the image model loaded and the run may not; the
    stored opinion keeps them from disagreeing about the same files."""
    root, dest = drive
    a_borderline_folder(root / "Downloads")
    estimate = ArchiveJob([str(root)], str(dest), vision=PictureJudge())
    list(estimate._walk())
    later = ArchiveJob([str(root)], str(dest), vision=None)
    list(later._walk())
    assert states(estimate) == states(later) == {"Agile project management": "held"}


def test_without_the_model_or_a_stored_opinion_the_pictures_add_nothing(drive):
    root, dest = drive
    a_borderline_folder(root / "Downloads")
    job = ArchiveJob([str(root)], str(dest), vision=None)
    list(job._walk())
    assert job.course_folders == []


def test_new_files_are_judged_afresh(drive):
    root, dest = drive
    folder = a_borderline_folder(root / "Downloads", dark=False)
    list(ArchiveJob([str(root)], str(dest), vision=PictureJudge())._walk())
    # The same folder now holds different pictures: the old answer no longer applies.
    for png in folder.rglob("*.png"):
        stream = io.BytesIO()
        Image.new("RGB", (64, 48), (20, 20, 20)).save(stream, "PNG")
        png.write_bytes(stream.getvalue() + b"changed")
    job = ArchiveJob([str(root)], str(dest), vision=PictureJudge())
    list(job._walk())
    assert states(job) == {"Agile project management": "held"}


def test_the_model_is_not_asked_about_folders_the_names_already_settle(drive):
    root, dest = drive
    a_course(root / "Materials", "[FreeTutorials.Us] Udemy - rest-api")
    a_trip(root / "Photos")
    judge = PictureJudge()
    list(ArchiveJob([str(root)], str(dest), vision=judge)._walk())
    assert judge.images_seen == 0
