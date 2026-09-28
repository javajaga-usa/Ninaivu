"""Regression tests for the audit of 25 September 2026.

See docs/audits/2026-09-25-audit/REPORT.md. The findings with a home of their
own are tested there: the backup schedule in test_backup.py, the restore in
test_cloud_restore.py, phone backup in test_phone_backup.py. These are the
request-shape findings, which cut across many routes.
"""

import json

import pytest

from conftest import ADMIN, FAMILY, login


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


@pytest.fixture()
def admin(app, people):
    client = login(app.test_client(), *ADMIN)
    client.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    return client


# -- #6: a string of ids was read one character at a time ---------------------

@pytest.mark.parametrize("url", ["/api/assets/bulk", "/api/delete",
                                 "/api/recycle/restore", "/api/recycle/purge"])
def test_ids_as_a_string_are_refused_not_taken_apart(admin, url):
    """"ids": "12" acted on items 1 and 2 — on the route that deletes, among
    others."""
    answer = post(admin, url, {"ids": "12", "favorite": True, "password": ADMIN[1]})
    assert answer.status_code == 400, answer.get_json()


def test_a_string_of_ids_changes_nothing(admin, people):
    conn = people["conn"]
    first, second = [r["id"] for r in conn.execute("SELECT id FROM assets ORDER BY id LIMIT 2")]
    post(admin, "/api/assets/bulk", {"ids": f"{first}{second}", "rating": 5})
    rated = conn.execute("SELECT COUNT(*) FROM user_assets WHERE rating=5").fetchone()[0]
    assert rated == 0


@pytest.mark.parametrize("url", ["/api/assets/bulk", "/api/delete",
                                 "/api/recycle/restore", "/api/recycle/purge",
                                 "/api/straighten/dismiss"])
def test_null_ids_are_no_ids_not_a_500(admin, url):
    assert post(admin, url, {"ids": None}).status_code < 500


def test_ids_as_a_list_still_work(admin, people):
    conn = people["conn"]
    first = conn.execute("SELECT id FROM assets ORDER BY id LIMIT 1").fetchone()[0]
    answer = post(admin, "/api/assets/bulk", {"ids": [first, str(first)], "rating": 4})
    assert answer.status_code == 200, answer.get_json()
    assert conn.execute("SELECT rating FROM user_assets WHERE asset_id=?",
                        (first,)).fetchone()[0] == 4


def test_family_members_are_held_to_the_same_shape(app, people):
    family = login(app.test_client(), *FAMILY)
    family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    assert post(family, "/api/assets/bulk", {"ids": "12", "favorite": True}).status_code == 400
    assert post(family, "/api/assets/bulk", {"ids": None}).status_code < 500


# -- #7: more malformed input that answered 500 --------------------------------

@pytest.mark.parametrize("url", ["/api/faces/clusters", "/api/faces/cluster/abc"])
def test_an_enormous_limit_is_not_a_500(admin, url):
    assert admin.get(url + "?limit=99999999999999999999").status_code < 500


def test_straightening_refuses_nonsense_with_a_400(admin):
    assert post(admin, "/api/straighten/apply", {"ids": "x"}).status_code == 400
    assert post(admin, "/api/straighten/apply", {"min_confidence": "x"}).status_code == 400
    assert post(admin, "/api/straighten/dismiss", {"ids": ["x"]}).status_code < 500


# -- #9: Gemini "analyze" read any library file whole into memory --------------

def test_gemini_is_asked_about_photographs_only(app, people, monkeypatch):
    """A video's id was read into memory whole — gigabytes, for one request —
    before anything checked that it was a photograph."""
    from ninaivu.media import gemini_media

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    sent = []
    monkeypatch.setattr(gemini_media, "analyze_image",
                        lambda image, opts: sent.append(len(image)) or {"caption": ""})
    conn = people["conn"]
    video, photo = [r["id"] for r in conn.execute("SELECT id FROM assets ORDER BY id LIMIT 2")]
    conn.execute("UPDATE assets SET kind='video' WHERE id=?", (video,))
    conn.commit()
    family = login(app.test_client(), *FAMILY)
    family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"

    refused = family.post("/api/ai-playground/gemini/analyze", json={"media_id": video})
    assert refused.status_code == 400 and sent == []
    allowed = family.post("/api/ai-playground/gemini/analyze", json={"media_id": photo})
    assert allowed.status_code == 200 and len(sent) == 1


# -- notification settings wiped by the console's own Save --------------------

def _console_save(client, form, events):
    """What the console's Save button posts: every field, as the form holds it."""
    return client.post("/api/admin/notifications", json={
        "webhook": form.get("webhook", ""), "smtp_host": form.get("smtp_host", ""),
        "smtp_port": form.get("smtp_port", 587), "smtp_user": form.get("smtp_user", ""),
        "smtp_password": "", "smtp_to": form.get("smtp_to", ""), "events": events})


def test_changing_a_tick_box_keeps_the_email_setup(admin, scanned):
    """The form opened empty and Save sent every field, so unticking one event
    wiped the webhook, the mail server and the password — and with them every
    warning about failing backups and disks."""
    cfg, _, _ = scanned
    admin.post("/api/admin/notifications", json={
        "webhook": "https://ntfy.sh/our-ninaivu", "smtp_host": "smtp.example.com",
        "smtp_user": "dad", "smtp_password": "hunter2", "smtp_to": "dad@example.com",
        "events": ["integrity", "cloud_stalled"]})

    shown = admin.get("/api/admin/notifications").get_json()
    assert "hunter2" not in json.dumps(shown)
    assert shown["form"]["smtp_password_saved"] is True
    _console_save(admin, shown["form"], ["integrity"])

    assert cfg.notify_webhook == "https://ntfy.sh/our-ninaivu"
    assert cfg.notify_smtp_host == "smtp.example.com"
    assert cfg.notify_smtp_to == "dad@example.com"
    assert cfg.notify_smtp_password == "hunter2", "a blank password field erased it"
    assert cfg.notify_events == ["integrity"]


def test_a_new_password_still_replaces_the_old(admin, scanned):
    cfg, _, _ = scanned
    admin.post("/api/admin/notifications", json={"smtp_password": "hunter2"})
    admin.post("/api/admin/notifications", json={"smtp_password": "correct-horse"})
    assert cfg.notify_smtp_password == "correct-horse"
