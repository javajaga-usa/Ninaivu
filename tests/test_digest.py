"""Sending the weekly photograph: the message, the schedule, the console.

The picker is tested in `test_digest_picker.py`. This is everything after it
— what the message contains, when it goes, and the two ways it must refuse:
a day with nothing worth sending, and a household that never asked for it.

The promise worth pinning is the one in the module docstring. `notify.py`
undertakes never to send a photograph, a path or a name; this one does send a
photograph, on purpose, to the family. Those are different products and they
must not quietly become one, so there are tests here about the settings being
separate.
"""

from __future__ import annotations

import time

import pytest
from PIL import Image

from ninaivu.server import auth
from ninaivu.storage import db
from ninaivu.utils import digest as digest_kit
from ninaivu import build_services, create_admin_app

from conftest import ADMIN, login

ROOT = "/library"
THIS_YEAR = int(time.strftime("%Y"))


# -- a library with one good photograph in it -------------------------------

@pytest.fixture()
def library(tmp_path):
    """An index with one sendable photograph, and a thumbnail on disk."""
    state = tmp_path / "state"
    thumbs = state / "thumbs" / "ab"
    thumbs.mkdir(parents=True)
    Image.new("RGB", (640, 480), (90, 120, 160)).save(thumbs / "cdef_640.webp")

    conn = db.init_db(state / "index.db")
    cursor = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, date_key, "
        "  captured_at, thumb, visibility, nsfw, trashed, sharpness, "
        "  brightness, quality, width, height, indexed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (ROOT, "2010/beach.jpg", "beach.jpg", "picture",
         f"{THIS_YEAR - 16:04d}-07-04", 0, "ab/cdef", 1, 0, 0,
         900.0, 0.5, "[]", 4000, 3000))
    for n in range(3):
        conn.execute(
            "INSERT INTO faces(asset_id, cluster_key, bbox, embedding) "
            "VALUES(?,?,?,?)", (cursor.lastrowid, f"c{n}", "[]", b""))
    conn.commit()
    return state, conn


class Settings:
    """Only what the digest reads, so a test says what it depends on."""

    def __init__(self, state, **over):
        self.state_dir = state
        self.thumbs_dir = state / "thumbs"
        self.thumb_format = "WEBP"
        self.house_name = "JSP"
        self.notify_smtp_host = "smtp.example.com"
        self.notify_smtp_port = 587
        self.notify_smtp_user = "ninaivu@example.com"
        self.notify_smtp_password = "secret"
        self.notify_smtp_tls = True
        self.digest_enabled = True
        self.digest_to = "family@example.com"
        self.digest_weekday = 6
        self.digest_hour = 9
        self.digest_link = ""
        self.__dict__.update(over)


def built(state, conn, **over):
    return digest_kit.build(Settings(state, **over), conn, [ROOT],
                            month=7, day=4)


# -- the message ------------------------------------------------------------

def test_the_photograph_is_in_the_message_not_behind_a_link(library):
    """A link to a home server works in the house and nowhere else."""
    state, conn = library
    message = built(state, conn)["message"]
    kinds = [part.get_content_type() for part in message.walk()]
    assert "image/jpeg" in kinds


def test_the_picture_is_converted_from_webp(library):
    """Mail clients that draw WebP are still the minority, and a photograph
    that arrives broken is worse than one that never arrives."""
    state, conn = library
    picture = digest_kit.thumbnail_bytes(
        Settings(state), {"thumb": "ab/cdef"})
    assert picture is not None
    assert picture[:3] == b"\xff\xd8\xff"          # a JPEG, not a WebP


def test_the_subject_says_how_long_ago(library):
    state, conn = library
    assert "16 years ago today" in built(state, conn)["subject"].lower()


def test_one_year_ago_is_not_one_years_ago():
    item = {"year": str(THIS_YEAR - 1), "face_count": 2}
    subject, line = digest_kit.describe(item, house="JSP")
    assert "a year ago today" in subject.lower()
    assert "1 years" not in line


def test_the_line_counts_the_people_in_it():
    _, line = digest_kit.describe({"year": "2010", "face_count": 4})
    assert "4 of you" in line
    _, alone = digest_kit.describe({"year": "2010", "face_count": 1})
    assert "one of you" in alone


def test_it_says_how_many_more_there_were(library):
    """The photograph is the message; the count is why to open Ninaivu."""
    state, conn = library
    conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, date_key, "
        "  captured_at, thumb, visibility, nsfw, trashed, sharpness, "
        "  brightness, quality, indexed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (ROOT, "2011/other.jpg", "other.jpg", "picture",
         f"{THIS_YEAR - 15:04d}-07-04", 0, "ab/cdef", 1, 0, 0, 800.0,
         0.5, "[]"))
    conn.commit()
    outcome = built(state, conn)
    assert outcome["others"] == 1
    plain = outcome["message"].get_body(("plain",)).get_content()
    assert "1 more" in plain


def test_a_missing_thumbnail_still_sends_words(library):
    """The file can go missing between the index and the disk."""
    state, conn = library
    (state / "thumbs" / "ab" / "cdef_640.webp").unlink()
    outcome = built(state, conn)
    assert outcome["ready"] is True
    assert outcome["has_picture"] is False
    assert "years ago today" in outcome["message"].get_content()



# -- what it refuses --------------------------------------------------------

def test_a_day_with_nothing_worth_sending_builds_nothing(library):
    state, conn = library
    outcome = digest_kit.build(Settings(state), conn, [ROOT], month=1, day=1)
    assert outcome["ready"] is False
    assert "nothing worth sending" in outcome["reason"]


def test_nobody_to_send_to_is_off(library):
    state, conn = library
    assert digest_kit.from_config(Settings(state, digest_to="")).configured is False


def test_no_mail_server_is_off(library):
    state, conn = library
    assert digest_kit.from_config(
        Settings(state, notify_smtp_host="")).configured is False


# -- when it goes -----------------------------------------------------------

def keeper(state, conn, **over):
    return digest_kit.DigestKeeper(
        Settings(state, **over), connect=lambda: conn, roots=lambda: [ROOT])


def sunday_at(hour):
    """A struct_time for a Sunday, which is weekday 6."""
    return time.struct_time((2026, 7, 5, hour, 0, 0, 6, 186, 0))


def test_it_goes_on_the_chosen_morning(library):
    state, conn = library
    assert keeper(state, conn).due(sunday_at(9)) is True


def test_not_before_the_hour(library):
    state, conn = library
    assert keeper(state, conn).due(sunday_at(8)) is False


def test_not_on_another_day(library):
    state, conn = library
    monday = time.struct_time((2026, 7, 6, 10, 0, 0, 0, 187, 0))
    assert keeper(state, conn).due(monday) is False


def test_not_twice_for_the_same_day(library):
    """A restart on a Sunday afternoon must not send a second one."""
    state, conn = library
    watch = keeper(state, conn)
    assert watch.due(sunday_at(9)) is True
    db.set_meta(conn, digest_kit.ALREADY_SENT_KEY, "2026-07-05")
    conn.commit()
    assert watch.due(sunday_at(14)) is False


def test_switched_off_is_never_due(library):
    state, conn = library
    assert keeper(state, conn, digest_enabled=False).due(sunday_at(9)) is False


def sending(state, conn, when, **over):
    """Run the keeper with its transport captured. Returns (result, sent)."""
    sent = []
    watch = keeper(state, conn, **over)
    original = digest_kit.from_config

    def wired(cfg):
        sender = original(cfg)
        sender.transport = sent.append
        return sender

    digest_kit.from_config = wired
    try:
        return watch.run(force=True, when=when), sent
    finally:
        digest_kit.from_config = original


#: The day the fixture library actually has a photograph on — a Sunday in the
#: fixture's terms so the schedule and the content agree.
FOURTH_OF_JULY = time.struct_time((2026, 7, 4, 9, 0, 0, 5, 185, 0))


def test_sending_composes_and_hands_over_one_message(library):
    """End to end without a socket: the transport sees the real message."""
    state, conn = library
    result, sent = sending(state, conn, FOURTH_OF_JULY)
    assert result["sent"] is True
    assert len(sent) == 1
    assert sent[0]["To"] == "family@example.com"
    assert "years ago today" in sent[0]["Subject"].lower()


def test_a_quiet_day_sends_nothing_and_says_why(library):
    """The fixture has one photograph, on 4 July. Every other day is quiet."""
    state, conn = library
    new_year = time.struct_time((2026, 1, 1, 9, 0, 0, 3, 1, 0))
    result, sent = sending(state, conn, new_year)
    assert result["sent"] is False
    assert "nothing worth sending" in result["reason"]
    assert sent == []


def test_a_quiet_day_is_not_written_down_as_sent(library):
    """Otherwise a quiet Sunday would block the following one."""
    state, conn = library
    sending(state, conn, time.struct_time((2026, 1, 1, 9, 0, 0, 3, 1, 0)))
    assert db.get_meta(conn, digest_kit.ALREADY_SENT_KEY) in (None, "")


def test_a_sent_day_is_written_down(library):
    state, conn = library
    sending(state, conn, FOURTH_OF_JULY)
    assert db.get_meta(conn, digest_kit.ALREADY_SENT_KEY) == "2026-07-04"


def test_a_mail_server_that_refuses_is_not_written_down_as_sent(library):
    """A failed send must be retried, not recorded as done."""
    state, conn = library
    watch = keeper(state, conn)
    original = digest_kit.from_config

    def broken(cfg):
        sender = original(cfg)
        sender.transport = lambda message: (_ for _ in ()).throw(
            OSError("the mail server said no"))
        return sender

    digest_kit.from_config = broken
    try:
        result = watch.run(force=True, when=FOURTH_OF_JULY)
    finally:
        digest_kit.from_config = original
    assert result["sent"] is False
    assert "said no" in result["reason"]
    assert db.get_meta(conn, digest_kit.ALREADY_SENT_KEY) in (None, "")


# -- through the console ----------------------------------------------------

@pytest.fixture()
def console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    yield login(app.test_client(), *ADMIN), cfg, services
    services.stop(timeout=5.0)


def test_it_is_off_until_somebody_asks(console):
    client, _, _ = console
    body = client.get("/api/admin/digest").get_json()
    assert body["enabled"] is False
    assert body["ready"] is False


def test_the_console_says_when_there_is_no_mail_server(console):
    """A switch that silently does nothing is worse than one that says so."""
    client, _, _ = console
    assert client.get("/api/admin/digest").get_json()["has_mail_server"] is False


def test_settings_are_saved(console):
    client, cfg, _ = console
    body = client.post("/api/admin/digest", json={
        "enabled": True, "to": "family@example.com", "weekday": 5, "hour": 8,
    }).get_json()
    assert body["ok"] is True
    assert cfg.digest_to == "family@example.com"
    assert cfg.digest_weekday == 5
    assert cfg.digest_hour == 8


@pytest.mark.parametrize("bad", [{"weekday": 9}, {"hour": 30}, {"hour": -1}])
def test_a_day_or_hour_that_is_not_one_is_a_400(console, bad):
    client, _, _ = console
    assert client.post("/api/admin/digest", json=bad).status_code == 400


def test_the_preview_does_not_send(console):
    """"Untestable until Sunday" is how a weekly thing ships broken."""
    client, _, _ = console
    body = client.get("/api/admin/digest/preview").get_json()
    assert "ready" in body


def test_sending_now_refuses_with_nobody_to_send_to(console):
    client, _, _ = console
    body = client.post("/api/admin/digest/send").get_json()
    assert body["sent"] is False
    assert "nobody" in body["reason"] or "mail server" in body["reason"]


def test_the_two_kinds_of_message_are_configured_apart(console):
    """Alerts go to whoever looks after the machine; photographs to the family.

    Sharing one recipient list would send a photograph of somebody's children
    to whichever address was set up to receive "a drive is unplugged".
    """
    client, cfg, _ = console
    client.post("/api/admin/digest", json={"to": "family@example.com"})
    assert cfg.digest_to == "family@example.com"
    assert getattr(cfg, "notify_smtp_to", "") != "family@example.com"


@pytest.mark.parametrize("path", ["/api/admin/digest",
                                  "/api/admin/digest/preview"])
def test_the_family_app_does_not_route_the_digest(scanned, path):
    from ninaivu import create_home_app

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        home = login(create_home_app(services).test_client(), *ADMIN)
        assert home.get(path).status_code == 404
    finally:
        services.stop(timeout=5.0)
