"""Telling somebody when the quiet things go wrong.

A server that runs all year fails silently by nature: the library looks fine
while the backup has not run since March and a photograph's bytes have
changed. Nobody is watching the console at the moment any of that happens.

Most of these tests are about restraint rather than delivery. A notifier that
sends too much is worse than none, because the messages stop being read — and
a notifier that leaks what is *in* the photographs would be worse still.
"""

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.utils import notify
from ninaivu.utils.notify import EVENTS, Notifier


class Spy:
    """Stands in for the network. Nothing here opens a socket."""

    def __init__(self):
        self.sent = []

    def __call__(self, channel, title, detail):
        self.sent.append((channel, title, detail))


@pytest.fixture()
def spy():
    return Spy()


def make(spy, **kw) -> Notifier:
    options = {"webhook_url": "https://example.invalid/hook",
               "transport": spy, "quiet_seconds": 0}
    options.update(kw)
    return Notifier(**options)


# --- off unless asked for --------------------------------------------------

def test_nothing_is_sent_until_somebody_configures_it():
    quiet = Notifier()
    assert quiet.configured is False
    assert quiet.send("integrity", "something")["sent"] is False


def test_a_default_install_has_no_endpoint():
    from ninaivu.server.config import Config
    assert notify.from_config(Config()).configured is False


# --- restraint -------------------------------------------------------------

def test_the_same_problem_is_not_reported_twice_in_a_row(spy):
    """A drive left unplugged over a weekend should produce one message, not
    one per poll."""
    notifier = make(spy, quiet_seconds=3600)
    assert notifier.send("archive_waiting", "waiting")["sent"] is True
    again = notifier.send("archive_waiting", "waiting")
    assert again["sent"] is False
    assert "recently" in again["reason"]
    assert len(spy.sent) == 1


def test_a_different_problem_still_gets_through(spy):
    """Silencing one thing must not silence the next."""
    notifier = make(spy, quiet_seconds=3600)
    notifier.send("archive_waiting", "waiting")
    assert notifier.send("integrity", "bytes changed")["sent"] is True
    assert len(spy.sent) == 2


def test_events_can_be_turned_off_individually(spy):
    notifier = make(spy, enabled_events=("integrity",))
    assert notifier.send("integrity", "yes")["sent"] is True
    assert notifier.send("scan_errors", "no")["sent"] is False


def test_an_unknown_event_is_never_sent(spy):
    assert make(spy).send("something_invented", "x")["sent"] is False


def test_a_test_message_ignores_the_quiet_window(spy):
    """Somebody pressing Test twice expects two messages."""
    notifier = make(spy, quiet_seconds=99999)
    notifier.test()
    notifier.test()
    assert len(spy.sent) == 2


# --- it must never break the thing it reports ------------------------------

def test_a_broken_endpoint_never_raises():
    """The caller is usually a background thread in the middle of doing the
    thing being reported. A notifier that throws turns 'the backup stalled'
    into 'the backup crashed'."""
    def explode(channel, title, detail):
        raise RuntimeError("the webhook is on fire")

    notifier = Notifier(webhook_url="https://example.invalid/hook",
                        transport=explode, quiet_seconds=0)
    result = notifier.send("integrity", "bytes changed")
    assert result["sent"] is False
    assert "fire" in str(result["webhook"])


def test_reporting_a_scrubber_finding_cannot_break_the_scrubber(monkeypatch):
    from ninaivu.api import admin_api

    def explode():
        raise RuntimeError("no notifier")
    monkeypatch.setattr(admin_api, "notifier", explode)
    admin_api.notify_event("integrity", "x")      # must simply return


def test_a_notification_that_cannot_be_sent_is_logged(monkeypatch, caplog):
    """Never raised to the caller -- but a broken webhook must not stay broken
    unnoticed for as long as nobody wonders why the notifications stopped."""
    import logging
    from ninaivu.api import admin_api

    class Broken:
        def send(self, *args):
            raise ConnectionError("webhook refused the connection")

    monkeypatch.setattr(admin_api, "notifier", lambda: Broken())
    with caplog.at_level(logging.WARNING):
        admin_api.notify_event("integrity", "2 files changed")
    assert any("integrity" in r.getMessage() and "webhook refused" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# --- what a message may contain --------------------------------------------

def test_the_house_name_is_used_so_you_know_which_server(spy):
    notifier = make(spy, house="The Kumar Home")
    notifier.send("integrity", "2 files changed")
    channel, title, _ = spy.sent[0]
    assert title.startswith("The Kumar Home:")


def test_only_events_a_person_would_act_on_exist():
    """No 'a scan finished'. That happens every night, and a notification
    that arrives every night is one nobody reads."""
    assert set(EVENTS) == {
        "integrity", "missing", "cloud_stalled", "cloud_failed",
        "archive_waiting", "scan_errors", "disk_low",
        # A drive that is failing, and a backup that did not restore.
        "disk_health", "restore_test"}


def test_a_scrubber_report_says_what_happened_without_naming_files(monkeypatch):
    """A notification travels further than the console does — through a phone,
    a chat channel, somebody's inbox. It says what and how many, never which
    photographs or where they live."""
    from ninaivu.api import admin_api

    captured = []
    monkeypatch.setattr(admin_api, "notify_event",
                        lambda *a, **k: captured.append(a))
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "corrupt", 2)
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "missing", 0)
    monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, "unreadable", 0)

    admin_api._report_scrubber_findings()

    assert captured, "a corrupt file was found and nobody was told"
    event, summary, detail = captured[0]
    assert event == "integrity"
    assert "2" in summary
    for leak in (".jpg", ".jpeg", "/", "\\\\"):
        assert leak not in detail, f"the message leaks a path: {detail}"


def test_a_clean_pass_says_nothing(monkeypatch):
    from ninaivu.api import admin_api

    captured = []
    monkeypatch.setattr(admin_api, "notify_event",
                        lambda *a, **k: captured.append(a))
    for key in ("corrupt", "missing", "unreadable"):
        monkeypatch.setitem(admin_api._SCRUBBER_PROGRESS, key, 0)

    admin_api._report_scrubber_findings()
    assert captured == []


# --- who may configure it --------------------------------------------------

@pytest.mark.parametrize("who", [FAMILY, GUEST])
def test_only_an_admin_may_see_or_change_notification_settings(app, people, who):
    client = app.test_client()
    login(client, *who)
    assert client.get("/api/admin/notifications").status_code in (401, 403, 404)
    assert client.post("/api/admin/notifications", json={}).status_code in (401, 403, 404)
    assert client.post("/api/admin/notifications/test").status_code in (401, 403, 404)


def test_an_admin_can_configure_and_the_settings_persist(app, people, scanned):
    cfg, _, _ = scanned
    client = app.test_client()
    login(client, *ADMIN)

    response = client.post("/api/admin/notifications", json={
        "webhook": "https://ntfy.sh/my-ninaivu",
        "webhook_format": "ntfy",
        "events": ["integrity", "cloud_stalled"],
        "quiet_hours": 12,
    })
    assert response.status_code == 200
    settings = response.get_json()["settings"]
    assert settings["configured"] is True
    assert settings["webhook_format"] == "ntfy"
    assert set(settings["events"]) == {"integrity", "cloud_stalled"}
    assert settings["quiet_hours"] == 12.0
    assert cfg.notify_webhook == "https://ntfy.sh/my-ninaivu"


def test_the_settings_never_hand_the_password_back(app, people):
    """It is write-only from the browser's point of view."""
    client = app.test_client()
    login(client, *ADMIN)
    client.post("/api/admin/notifications", json={
        "smtp_host": "smtp.example.com", "smtp_to": "dad@example.com",
        "smtp_password": "hunter2"})

    body = client.get("/api/admin/notifications").get_data(as_text=True)
    assert "hunter2" not in body
