"""A phone that locks itself part-way through an import.

An iPhone hides its storage the moment it locks. On the import this was found
on, every file after that point failed at once — 453 of them in one minute,
each logged as a copy that failed — and the archive run then said "complete"
with most of the camera roll still on the phone. Three more tries that
evening went the same way.

What must happen instead: the import waits for the phone and carries on from
the file it was on; if the phone does not come back it stops and says so, and
the run is INCOMPLETE rather than complete. A folder that really has gone from
a phone that is still there is stepped over as before, and a copy cut off
part-way is fetched again rather than archived short.

The shell is replaced by a fake phone, as in test_devices.py.
"""

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from ninaivu import archive
from ninaivu.archive import database as db
from ninaivu.archive import scanner
from ninaivu.utils import devices

ROOT = "This PC\\Apple iPhone\\Internal Storage"


def _jpeg(shade):
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (shade, 90, 200)).save(out, "JPEG")
    return out.getvalue()


class Item:
    def __init__(self, name, folder=None, data=b""):
        self.Name = name
        self.GetFolder = folder
        self.IsFolder = folder is not None
        self.data = data

    def ExtendedProperty(self, name):
        assert name == "System.Size"
        return len(self.data)


class Folder:
    def __init__(self, items):
        self.items = items

    def Items(self):
        return list(self.items)

    def GetDetailsOf(self, item, column):
        if item is None:
            return ""
        return {0: item.Name, 1: f"{max(1, len(item.data) // 1024)} KB"}.get(column, "")


class Landing:
    """Where the shell copies to. Like the real one, it reports nothing."""

    def __init__(self, folder, phone):
        self.folder, self.phone = Path(folder), phone

    def CopyHere(self, item, flags):
        phone = self.phone
        phone.asked.append(item.Name)
        if len(phone.asked) == phone.lock_at_request:
            phone.lock()
        if phone.locked:
            return                      # nothing arrives from a locked phone
        (self.folder / item.Name).write_bytes(item.data)
        phone.arrived += 1
        if phone.arrived == phone.lock_after_arrivals:
            phone.lock()
        if phone.arrived == phone.remove_after_arrivals:
            # The month not reached yet: the walk takes the last one first.
            phone.storage.GetFolder.items.pop(0)


class Phone:
    """An iPhone with two months of camera roll, that can lock itself."""

    def __init__(self):
        months = []
        shade = 0
        for month in ("202511_a", "202512_a"):
            files = []
            for _ in range(3):
                shade += 1
                files.append(Item(f"IMG_{shade:04}.JPG", data=_jpeg(shade * 40)))
            months.append(Item(month, Folder(files)))
        self.storage = Item("Internal Storage", Folder(months))
        self.device = Folder([self.storage])
        self.computer = Folder([Item("Apple iPhone", self.device)])
        self.locked = False
        self.asked = []
        self.arrived = 0
        self.lock_at_request = None
        self.lock_after_arrivals = None
        self.remove_after_arrivals = None

    def lock(self):
        """A locked iPhone is still under This PC, with nothing inside it."""
        self.locked = True
        self.device.items = []

    def unlock(self):
        self.locked = False
        self.device.items = [self.storage]

    def NameSpace(self, what):
        if what == devices.SSF_DRIVES:
            return self.computer
        return Landing(what, self)


@pytest.fixture()
def iphone(monkeypatch):
    monkeypatch.setattr(devices, "_FILE_TIMEOUT", 3.0)
    monkeypatch.setattr(devices, "_SETTLE", 0.01)
    monkeypatch.setattr(devices, "_STALL", 0.2, raising=False)
    monkeypatch.setattr(devices, "_DEVICE_POLL", 0.05, raising=False)
    monkeypatch.setattr(devices, "_DEVICE_WAIT", 1.0, raising=False)
    phone = Phone()
    monkeypatch.setattr(devices, "unavailable_reason", lambda: None)
    monkeypatch.setattr(devices, "_shell", lambda: phone)
    return phone


def copy(landing, **kwargs):
    return devices.copy_out(ROOT, landing, preserve_structure=True, **kwargs)


# ---------------------------------------------------------------------------
# The copy off the phone
# ---------------------------------------------------------------------------

def test_unlocked_when_asked_it_carries_on_from_the_same_file(iphone, tmp_path):
    iphone.lock_at_request = 3
    told = []

    def waiting(where):
        told.append(where)
        iphone.unlock()                  # somebody picks the phone up

    result = copy(tmp_path / "in", waiting=waiting)
    assert result["copied"] == 6 and result["failed"] == 0
    assert not result["device_lost"]
    assert len(told) == 1
    assert iphone.asked.count(iphone.asked[2]) == 2, "the file it was on was not tried again"


def test_a_phone_that_never_comes_back_is_not_a_run_of_failures(iphone, tmp_path):
    iphone.lock_at_request = 3
    result = copy(tmp_path / "in")
    assert result["device_lost"] is True
    assert result["copied"] == 2
    assert result["failed"] == 0, "files never asked for were counted as failed copies"
    assert len(iphone.asked) == 3, "a locked phone went on being asked for files"


def test_locked_between_folders_is_not_the_end_of_the_camera_roll(iphone, tmp_path):
    """The walk stepped over every folder it could not read — with a locked
    phone, that was every folder it had not reached yet, and the copy then
    looked finished."""
    iphone.lock_after_arrivals = 3
    result = copy(tmp_path / "in")
    assert result["copied"] == 3
    assert result["device_lost"] is True


def test_locked_between_folders_and_unlocked_gets_everything(iphone, tmp_path):
    iphone.lock_after_arrivals = 3
    result = copy(tmp_path / "in", waiting=lambda where: iphone.unlock())
    assert result["copied"] == 6 and not result["device_lost"]


def test_a_folder_gone_from_a_phone_that_is_there_is_stepped_over(iphone, tmp_path):
    iphone.remove_after_arrivals = 3
    told = []
    started = time.monotonic()
    result = copy(tmp_path / "in", waiting=told.append)
    assert result["copied"] == 3 and not result["device_lost"]
    assert told == [], "waited for a phone that had not gone anywhere"
    assert time.monotonic() - started < devices._DEVICE_WAIT


def test_a_copy_cut_off_part_way_is_fetched_again(iphone, tmp_path):
    landing = tmp_path / "in"
    assert copy(landing)["copied"] == 6
    short = next(landing.rglob("IMG_0001.JPG"))
    whole = short.stat().st_size
    short.write_bytes(short.read_bytes()[:100])      # the phone locked mid-file

    again = copy(landing)
    assert again["copied"] == 1 and again["skipped"] == 5
    assert short.stat().st_size == whole


def test_stopping_while_it_waits_is_not_a_lost_phone(iphone, tmp_path):
    iphone.lock_at_request = 2
    stop = {"now": False}
    result = copy(tmp_path / "in", should_stop=lambda: stop["now"],
                  waiting=lambda where: stop.update(now=True))
    assert not result["device_lost"]


# ---------------------------------------------------------------------------
# The archive run that stages from it
# ---------------------------------------------------------------------------

@pytest.fixture()
def work(tmp_path):
    archive.configure(tmp_path / "state")
    db.close_db()
    db.init_db()
    scanner._job = None
    scanner.forget_estimate()
    try:
        yield tmp_path
    finally:
        scanner.stop_scan()
        if scanner._job is not None and scanner._job.thread is not None:
            scanner._job.gate.resume()
            scanner._job.thread.join(timeout=30)
        scanner._job = None
        scanner.forget_estimate()
        db.close_db()


def run(dest):
    ok, problems, _ = scanner.start_scan([ROOT], str(dest))
    assert ok, problems
    job = scanner._job
    for _ in range(600):
        try:
            job.thread.join(timeout=0.1)
        except RuntimeError:
            time.sleep(0.05)
            continue
        if not job.thread.is_alive():
            return db.latest_job()
    raise AssertionError("the run did not finish")


def archived(dest):
    return [p for p in dest.rglob("*.JPG") if ".ninaivu_staging" not in p.parts]


def test_the_run_says_incomplete_and_what_to_do(work, iphone):
    iphone.lock_at_request = 3
    dest = work / "dest"
    job = run(dest)
    assert job["state"] == "completed"
    assert job["message"].startswith("INCOMPLETE"), job["message"]
    assert "stopped answering" in job["message"]
    assert "press Start again" in job["message"]
    assert len(archived(dest)) == 2, "what did come off the phone was not archived"


def test_the_next_run_fetches_only_what_is_left(work, iphone):
    """Staged files are removed as they are archived, so the next run used to
    fetch the whole camera roll again only to find most of it done."""
    iphone.lock_at_request = 3
    dest = work / "dest"
    run(dest)
    iphone.unlock()
    asked_before = len(iphone.asked)

    job = run(dest)
    assert job["message"].startswith("complete"), job["message"]
    assert len(iphone.asked) - asked_before == 4
    assert len(archived(dest)) == 6
    assert not (dest / ".ninaivu_staging").exists()


def test_an_import_that_went_well_still_says_complete(work, iphone):
    job = run(work / "dest")
    assert job["message"].startswith("complete"), job["message"]


def test_a_photograph_from_a_phone_is_not_archived_hidden(work, iphone):
    """Ninaivu's own staging folder starts with a dot, which was taken for the
    person hiding the photograph: every one imported from a phone — 67 on the
    machine this was found on — was archived with the hidden flag set."""
    dest = work / "dest"
    run(dest)
    assert len(archived(dest)) == 6
    assert not any(scanner.source_is_hidden(str(p)) for p in archived(dest))


def test_the_staging_folder_is_not_the_person_hiding_it(tmp_path):
    staged = tmp_path / ".ninaivu_staging" / "This_PC_Apple_iPhone" / "202511_a" / "IMG_1.JPG"
    assert scanner.source_is_hidden(str(staged)) is False


def test_a_folder_hidden_on_the_phone_still_counts(tmp_path):
    staged = tmp_path / ".ninaivu_staging" / "This_PC_Galaxy" / ".private" / "IMG_2.JPG"
    assert scanner.source_is_hidden(str(staged)) is True
