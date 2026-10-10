"""The phone inbox: a sync app sending photographs home with a phone key.

Driven the way PhotoSync and its kind drive it: HTTP Basic with the person's
username and a phone key, MKCOL for the app's folders, PUT for each file,
PROPFIND or HEAD to see what is there.
"""

import base64
import io
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.api import api_webdav
from ninaivu.media import phone_backup, phone_keys
from ninaivu.server import auth


def photo(colour=(200, 40, 40), size=(640, 480)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def basic(username, key):
    token = base64.b64encode(f"{username}:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(autouse=True)
def _no_filing_left_behind():
    yield
    api_webdav.cancel_pending()


@pytest.fixture()
def family(app, people):
    return login(app.test_client(), *FAMILY)


@pytest.fixture()
def admin(app, people):
    return login(app.test_client(), *ADMIN)


@pytest.fixture()
def key(family):
    made = family.post("/api/phone-keys", json={"name": "Maya's iPhone"})
    assert made.status_code == 201, made.get_json()
    return made.get_json()["key"]


@pytest.fixture()
def phone(app, key):
    """A sync app: no cookies, only the key."""
    client = app.test_client()
    client.auth_headers = basic(FAMILY[0], key)
    return client


def put(client, path, data, **headers):
    return client.put(f"/dav/{path}", data=data,
                      headers={**client.auth_headers, **headers})


def pending(conn):
    return conn.execute("SELECT * FROM pending_uploads WHERE status='pending'").fetchall()


# -- keys -------------------------------------------------------------------------

def test_a_key_is_shown_once_and_listed_by_its_last_characters(family, key):
    listed = family.get("/api/phone-keys").get_json()
    assert key.startswith(phone_keys.PREFIX)
    assert [k["name"] for k in listed["keys"]] == ["Maya's iPhone"]
    assert listed["keys"][0]["hint"] == key[-4:]
    assert key not in str(listed), "the key itself must never be listed again"
    assert listed["webdav_url"].endswith("/dav/")


def test_the_key_is_kept_as_a_hash(key, people):
    stored = people["conn"].execute("SELECT key_hash FROM phone_keys").fetchone()[0]
    assert stored != key and key not in stored


def test_guests_cannot_make_a_key(app, people):
    guest = login(app.test_client(), *GUEST)
    assert guest.post("/api/phone-keys", json={"name": "x"}).status_code == 403


def test_a_person_cannot_revoke_somebody_elses_key(admin, family, key):
    mine = family.get("/api/phone-keys").get_json()["keys"][0]["id"]
    assert admin.delete(f"/api/phone-keys/{mine}").status_code == 404
    # The console can.
    assert admin.delete(f"/api/admin/phone-keys/{mine}").status_code == 200


# -- signing in -------------------------------------------------------------------

def test_without_a_key_the_inbox_asks_for_one(app, people):
    answer = app.test_client().open("/dav/", method="PROPFIND")
    assert answer.status_code == 401
    assert "Basic" in answer.headers["WWW-Authenticate"]


def test_a_signed_in_browser_cannot_use_the_inbox(family, key):
    answer = family.put("/dav/IMG_0001.png", data=photo())
    assert answer.status_code == 401


def test_a_wrong_or_revoked_key_is_refused(app, family, phone, key):
    wrong = app.test_client().open("/dav/", method="PROPFIND",
                                   headers=basic(FAMILY[0], key[:-1] + "x"))
    assert wrong.status_code == 401
    assert phone.open("/dav/", method="PROPFIND",
                      headers=phone.auth_headers).status_code == 207
    key_id = family.get("/api/phone-keys").get_json()["keys"][0]["id"]
    family.delete(f"/api/phone-keys/{key_id}")
    assert phone.open("/dav/", method="PROPFIND",
                      headers=phone.auth_headers).status_code == 401


def test_a_key_stops_when_its_profile_becomes_a_guest(phone, people):
    conn = people["conn"]
    conn.execute("UPDATE users SET role=? WHERE id=?", (auth.ROLE_GUEST, people["family"].id))
    conn.commit()
    assert put(phone, "IMG_0001.png", photo()).status_code == 401


def test_a_deleted_profiles_keys_go_with_it(phone, people):
    auth.delete_user(people["conn"], people["family"].id)
    assert people["conn"].execute("SELECT COUNT(*) FROM phone_keys").fetchone()[0] == 0


def test_the_inbox_does_not_answer_the_internet(app, phone):
    answer = phone.put("/dav/IMG_0001.png", data=photo(), headers=phone.auth_headers,
                       environ_base={"REMOTE_ADDR": "8.8.8.8"})
    assert answer.status_code == 403


def test_options_answers_without_a_key(app, people):
    answer = app.test_client().open("/dav/", method="OPTIONS")
    assert answer.status_code == 200 and answer.headers["DAV"] == "1"


# -- sending ----------------------------------------------------------------------

def test_a_file_sent_whole_waits_for_review(phone, people):
    data = photo()
    assert phone.open("/dav/2024/05", method="MKCOL",
                      headers=phone.auth_headers).status_code == 201
    sent = put(phone, "2024/05/IMG_0001.png", data)
    assert sent.status_code == 201, sent.data

    rows = pending(people["conn"])
    assert len(rows) == 1 and rows[0]["filename"] == "IMG_0001.png"
    backup = people["conn"].execute("SELECT * FROM phone_backups").fetchone()
    assert backup["state"] == phone_backup.STAGED and backup["device"] == "Maya's iPhone"
    state = Path(phone.application.config["MV_CONFIG"].state_dir) / "phone-backup"
    assert not list(state.glob("*.part")) and not list(state.glob("incoming-*"))


def test_what_was_sent_is_seen_and_not_taken_twice(phone, people):
    data = photo()
    put(phone, "2024/05/IMG_0002.png", data)

    head = phone.head("/dav/2024/05/IMG_0002.png", headers=phone.auth_headers)
    assert head.status_code == 200 and int(head.headers["Content-Length"]) == len(data)
    listing = phone.open("/dav/2024/05/", method="PROPFIND",
                         headers={**phone.auth_headers, "Depth": "1"})
    assert listing.status_code == 207
    assert b"IMG_0002.png" in listing.data
    assert f"<D:getcontentlength>{len(data)}".encode() in listing.data
    top = phone.open("/dav/", method="PROPFIND", headers={**phone.auth_headers, "Depth": "1"})
    assert b"/dav/2024/" in top.data and b"IMG_0002.png" not in top.data
    assert phone.head("/dav/2024/05/IMG_9999.png",
                      headers=phone.auth_headers).status_code == 404

    again = put(phone, "2024/05/IMG_0002.png", data)
    assert again.status_code == 204
    assert len(pending(people["conn"])) == 1


def test_a_photo_already_in_the_library_is_not_stored_twice(phone, people):
    cfg = phone.application.config["MV_CONFIG"]
    original = (Path(cfg.active_root) / "misc" / "plain.png").read_bytes()
    assert put(phone, "copy-of-plain.png", original).status_code == 201
    assert pending(people["conn"]) == []
    assert people["conn"].execute(
        "SELECT state FROM phone_backups").fetchone()[0] == phone_backup.DUPLICATE


def test_only_photographs_video_and_audio_are_taken(phone):
    assert put(phone, "notes.pdf", b"%PDF-1.4").status_code == 415


def test_an_empty_file_is_refused(phone):
    # Refused as empty, or for saying no length at all, which is how the test
    # client sends an empty body.
    assert put(phone, "IMG_0003.png", b"").status_code in (400, 411)


def test_a_key_cannot_read_or_delete(phone):
    put(phone, "IMG_0004.png", photo())
    assert phone.get("/dav/IMG_0004.png", headers=phone.auth_headers).status_code == 403
    assert phone.delete("/dav/IMG_0004.png", headers=phone.auth_headers).status_code == 403


def test_a_rename_moves_only_the_remembered_path(phone):
    put(phone, "upload/IMG_0005.png", photo())
    moved = phone.open("/dav/upload/IMG_0005.png", method="MOVE",
                       headers={**phone.auth_headers,
                                "Destination": "http://localhost/dav/2024/IMG_0005.png"})
    assert moved.status_code == 201
    assert phone.head("/dav/2024/IMG_0005.png", headers=phone.auth_headers).status_code == 200
    assert phone.head("/dav/upload/IMG_0005.png",
                      headers=phone.auth_headers).status_code == 404


def test_a_dates_change_after_sending_is_accepted(phone):
    put(phone, "IMG_0006.png", photo())
    answer = phone.open("/dav/IMG_0006.png", method="PROPPATCH", headers=phone.auth_headers,
                        data=b"<?xml version='1.0'?><propertyupdate xmlns='DAV:'/>")
    assert answer.status_code == 207


def test_each_key_counts_what_it_sent(family, phone):
    put(phone, "IMG_0007.png", photo())
    put(phone, "IMG_0008.png", photo((10, 200, 10)))
    listed = family.get("/api/phone-keys").get_json()["keys"][0]
    assert listed["files"] == 2 and listed["last_used_at"] > 0


# -- into the library ---------------------------------------------------------------

def _wait_for(check, seconds=10.0):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_trusted_backups_are_filed_once_the_phone_is_quiet(monkeypatch, admin, phone, people):
    monkeypatch.setattr(api_webdav, "FILE_AFTER", 0.2)
    admin.post("/api/admin/phone-backups/settings", json={"trusted": True})
    put(phone, "IMG_0010.png", photo((30, 30, 200)))
    put(phone, "IMG_0011.png", photo((30, 200, 30)))

    def filed():
        return people["conn"].execute(
            "SELECT COUNT(*) FROM assets WHERE filename IN ('IMG_0010.png','IMG_0011.png')"
        ).fetchone()[0] == 2
    assert _wait_for(filed), "the trusted backups were never filed"
    assert pending(people["conn"]) == []


def test_untrusted_backups_wait_for_the_administrator(monkeypatch, phone, people):
    monkeypatch.setattr(api_webdav, "FILE_AFTER", 0.05)
    put(phone, "IMG_0012.png", photo())
    time.sleep(0.3)
    assert len(pending(people["conn"])) == 1


def test_a_phone_over_its_allowance_is_told_to_try_later(monkeypatch, phone, people):
    from ninaivu.media import upload_review
    phone.application.config["MV_CONFIG"].upload_quota_gb = 1
    monkeypatch.setattr(upload_review, "waiting_bytes", lambda conn, user_id: 1024 ** 3)
    answer = put(phone, "IMG_0020.png", photo())
    assert answer.status_code == 507, "a sync app retries a 507; a 415 it gives up on"
    assert pending(people["conn"]) == []
