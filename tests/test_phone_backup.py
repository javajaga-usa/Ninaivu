"""Backing a phone up to Ninaivu: resumable, checked, and never stored twice.

Driven through the family app's routes with the test client, the way the page
drives them: ask what is already here, start a file, send it in pieces.
"""

import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.media import phone_backup

DEVICE = "phone-test-0001"
TAKEN_MS = 1_500_000_000_000          # 2017-07-14, as a phone reports it


def photo(colour=(200, 40, 40), size=(640, 480)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def offer(client, name, data, modified=TAKEN_MS):
    return client.post("/api/phone-backup/files", json={
        "device_id": DEVICE, "device": "Maya's phone", "name": name,
        "size": len(data), "modified": modified})


def send(client, row_id, data, offset=0, piece=4096):
    answer = None
    while offset < len(data):
        chunk = data[offset:offset + piece]
        answer = client.put(f"/api/phone-backup/files/{row_id}?offset={offset}",
                            data=chunk, content_type="application/octet-stream")
        assert answer.status_code == 200, answer.get_json()
        offset = answer.get_json()["offset"]
    return answer.get_json()


def back_up(client, name, data, **kwargs):
    started = offer(client, name, data, **kwargs)
    assert started.status_code == 200, started.get_json()
    return send(client, started.get_json()["id"], data)


@pytest.fixture()
def family(app, people):
    return login(app.test_client(), *FAMILY)


@pytest.fixture()
def admin(app, people):
    return login(app.test_client(), *ADMIN)


def pending(conn):
    return conn.execute("SELECT * FROM pending_uploads WHERE status='pending'").fetchall()


# -- the round trip ---------------------------------------------------------------

def test_a_photo_arrives_whole_and_waits_for_review(family, people):
    data = photo()
    done = back_up(family, "IMG_0001.png", data)

    assert done["state"] == phone_backup.STAGED
    rows = pending(people["conn"])
    assert len(rows) == 1 and rows[0]["filename"] == "IMG_0001.png"
    staged = (Path(family.application.config["MV_CONFIG"].state_dir)
              / "pending-uploads" / rows[0]["storage_key"] / "IMG_0001.png")
    assert staged.read_bytes() == data
    assert not list((Path(family.application.config["MV_CONFIG"].state_dir)
                     / "phone-backup").glob("*.part")), "the partial file was left"


def test_it_keeps_the_phones_date_for_a_photo_with_none_inside(family, people):
    back_up(family, "Screenshot.png", photo())
    staged = (Path(family.application.config["MV_CONFIG"].state_dir) / "pending-uploads"
              / pending(people["conn"])[0]["storage_key"] / "Screenshot.png")
    assert abs(staged.stat().st_mtime - TAKEN_MS / 1000) < 2


def test_what_is_backed_up_is_not_sent_again(family):
    data = photo()
    back_up(family, "IMG_0002.png", data)

    checked = family.post("/api/phone-backup/check", json={
        "device_id": DEVICE,
        "files": [{"name": "IMG_0002.png", "size": len(data), "modified": TAKEN_MS},
                  {"name": "IMG_0003.png", "size": 10, "modified": TAKEN_MS},
                  {"name": "notes.pdf", "size": 10, "modified": TAKEN_MS}]}).get_json()

    assert [f["state"] for f in checked["files"]] == ["staged", "new", "unsupported"]
    assert checked["summary"]["this_phone"]["safe"] == 1
    again = offer(family, "IMG_0002.png", data).get_json()
    assert again["state"] == "staged", "offering it again must not start it again"


def test_an_interrupted_file_carries_on_from_the_byte_it_reached(family):
    data = os.urandom(20_000) + photo()
    row = offer(family, "VID_0001.png", data).get_json()
    family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=data[:8192],
               content_type="application/octet-stream")

    # The tab closed. Next time, the same file is offered again.
    resumed = offer(family, "VID_0001.png", data).get_json()
    assert resumed["id"] == row["id"] and resumed["offset"] == 8192
    assert send(family, row["id"], data, offset=8192)["state"] == "staged"


def test_a_piece_out_of_step_is_told_where_to_carry_on(family):
    data = photo()
    row = offer(family, "IMG_0004.png", data).get_json()
    family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=data[:1000],
               content_type="application/octet-stream")
    # The answer to that piece was lost, so the phone sends it again.
    repeat = family.put(f"/api/phone-backup/files/{row['id']}?offset=0",
                        data=data[:1000], content_type="application/octet-stream")
    assert repeat.status_code == 409 and repeat.get_json()["offset"] == 1000


def test_more_bytes_than_were_promised_are_refused(family):
    data = photo()
    row = offer(family, "IMG_0005.png", data).get_json()
    too_much = family.put(f"/api/phone-backup/files/{row['id']}?offset=0",
                          data=data + b"extra", content_type="application/octet-stream")
    assert too_much.status_code == 400


def test_a_photo_already_in_the_library_is_not_stored_twice(family, people):
    cfg = family.application.config["MV_CONFIG"]
    original = (Path(cfg.active_root) / "misc" / "plain.png").read_bytes()
    done = back_up(family, "copy-of-plain.png", original)
    assert done["state"] == phone_backup.DUPLICATE
    assert pending(people["conn"]) == []


def test_something_that_is_not_a_photo_is_refused(family):
    started = family.post("/api/phone-backup/files", json={
        "device_id": DEVICE, "name": "virus.exe", "size": 10, "modified": TAKEN_MS})
    assert started.status_code == 400


# -- whose files are whose ------------------------------------------------------------

def test_one_person_cannot_write_into_anothers_backup(app, family, admin):
    data = photo()
    row = offer(family, "IMG_0006.png", data).get_json()
    other = admin.put(f"/api/phone-backup/files/{row['id']}?offset=0",
                      data=data, content_type="application/octet-stream")
    assert other.status_code == 404


def test_guests_cannot_back_up(app, people):
    guest = login(app.test_client(), *GUEST)
    assert offer(guest, "IMG_0007.png", photo()).status_code in (401, 403)


def test_a_malformed_phone_id_is_refused(family):
    answer = family.post("/api/phone-backup/check",
                         json={"device_id": "../../etc", "files": []})
    assert answer.status_code == 400


# -- into the library ---------------------------------------------------------------

def test_a_family_phone_waits_for_review_unless_trusted(family, admin, people):
    back_up(family, "IMG_0008.png", photo())
    finished = family.post("/api/phone-backup/finish", json={"device_id": DEVICE}).get_json()
    assert finished["needs_review"] and finished["approved"] == 0

    admin.post("/api/admin/phone-backups/settings", json={"trusted": True})
    back_up(family, "IMG_0009.png", photo((10, 200, 10)))
    finished = family.post("/api/phone-backup/finish", json={"device_id": DEVICE}).get_json()
    assert not finished["needs_review"] and finished["approved"] == 2
    assert finished["summary"]["this_phone"]["in_library"] == 2
    names = {r["filename"] for r in people["conn"].execute(
        "SELECT filename FROM assets WHERE filename LIKE 'IMG_000%'")}
    assert names == {"IMG_0008.png", "IMG_0009.png"}


def test_an_administrator_approves_a_whole_phone_at_once(family, admin, people):
    for n in range(3):
        back_up(family, f"IMG_01{n}.png", photo((n * 60, 90, 90)))
    overview = admin.get("/api/admin/phone-backups").get_json()
    maya = next(p for p in overview["people"] if p["user_id"] == people["family"].id)
    assert maya["waiting"] == 3 and maya["phones"] == 1

    approved = admin.post(f"/api/admin/phone-backups/{people['family'].id}/approve").get_json()
    assert approved["approved"] == 3
    status = family.get(f"/api/phone-backup/status?device_id={DEVICE}").get_json()
    assert status["this_phone"]["in_library"] == 3
    assert status["this_phone"]["waiting_review"] == 0


def test_an_administrators_own_phone_goes_straight_in(admin, people):
    back_up(admin, "IMG_0100.png", photo())
    finished = admin.post("/api/phone-backup/finish", json={"device_id": DEVICE}).get_json()
    assert finished["approved"] == 1
    assert people["conn"].execute(
        "SELECT 1 FROM assets WHERE filename='IMG_0100.png'").fetchone()


def test_a_review_done_in_the_console_reaches_the_phone(family, admin, people):
    back_up(family, "IMG_0200.png", photo())
    upload_id = pending(people["conn"])[0]["id"]
    admin.post(f"/api/admin/uploads/{upload_id}/approve", json={})
    status = family.get(f"/api/phone-backup/status?device_id={DEVICE}").get_json()
    assert status["this_phone"]["in_library"] == 1
    assert status["recent"][0]["state"] == "done"


def test_the_summary_counts_what_is_safe(family):
    back_up(family, "IMG_0300.png", photo())
    row = offer(family, "IMG_0301.png", photo((1, 2, 3))).get_json()
    family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=b"\x89PNG",
               content_type="application/octet-stream")
    summary = family.get(f"/api/phone-backup/status?device_id={DEVICE}").get_json()
    phone = summary["this_phone"]
    assert phone["safe"] == 1 and phone["partial"] == 1 and phone["files"] == 2
    assert summary["phones"][0]["device"] == "Maya's phone"
    assert time.time() - phone["last_backup"] < 60


def test_a_partial_file_that_went_missing_starts_again_honestly(family):
    """The state folder was tidied between two evenings of backing up."""
    data = os.urandom(20_000)
    row = offer(family, "VID_0900.mp4", data).get_json()
    family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=data[:8192],
               content_type="application/octet-stream")
    state = Path(family.application.config["MV_CONFIG"].state_dir)
    (state / "phone-backup" / f"{row['id']}.part").unlink()

    resumed = offer(family, "VID_0900.mp4", data).get_json()
    assert resumed["offset"] == 0


# -- the disk the index lives on ---------------------------------------------------

def test_space_is_asked_about_for_every_piece(family, monkeypatch):
    """Checked only when a file was offered, two phones sending long videos
    each passed that check against the same free space, and between them
    could fill the disk the library's index is on."""
    data = os.urandom(20_000)
    row = offer(family, "VID_0901.mp4", data).get_json()
    first = family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=data[:8192],
                       content_type="application/octet-stream")
    assert first.status_code == 200

    real = phone_backup.shutil.disk_usage

    class Nearly:
        def __init__(self, path):
            usage = real(path)
            self.total, self.used = usage.total, usage.used
            self.free = phone_backup.KEEP_FREE_BYTES + 100

    monkeypatch.setattr(phone_backup.shutil, "disk_usage", Nearly)
    refused = family.put(f"/api/phone-backup/files/{row['id']}?offset=8192",
                         data=data[8192:16384], content_type="application/octet-stream")
    assert refused.status_code == 507
    assert refused.get_json()["offset"] == 8192, "the phone was not told where it got to"


def test_a_piece_too_large_is_refused_before_it_is_read(family, monkeypatch):
    monkeypatch.setattr(phone_backup, "MAX_PIECE_BYTES", 1000)
    data = os.urandom(5000)
    row = offer(family, "VID_0902.mp4", data).get_json()
    too_big = family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=data[:2000],
                         content_type="application/octet-stream")
    assert too_big.status_code == 413


def test_a_backup_nobody_came_back_to_is_cleared_after_a_month(family, people):
    """Its part sat in the state folder, on the index's disk, for good."""
    data = os.urandom(20_000)
    row = offer(family, "VID_0903.mp4", data).get_json()
    family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=data[:8192],
               content_type="application/octet-stream")
    cfg = family.application.config["MV_CONFIG"]
    part = Path(cfg.state_dir) / "phone-backup" / f"{row['id']}.part"
    month_ago = time.time() - phone_backup.ABANDONED_AFTER - 60
    os.utime(part, (month_ago, month_ago))

    assert phone_backup.sweep_abandoned(people["conn"], cfg) == 1
    assert not part.exists()
    again = offer(family, "VID_0903.mp4", data).get_json()
    assert again["state"] == phone_backup.RECEIVING and again["offset"] == 0


def test_a_backup_still_arriving_is_not_cleared(family, people):
    data = os.urandom(20_000)
    row = offer(family, "VID_0904.mp4", data).get_json()
    family.put(f"/api/phone-backup/files/{row['id']}?offset=0", data=data[:8192],
               content_type="application/octet-stream")
    cfg = family.application.config["MV_CONFIG"]
    assert phone_backup.sweep_abandoned(people["conn"], cfg) == 0
    assert (Path(cfg.state_dir) / "phone-backup" / f"{row['id']}.part").exists()
