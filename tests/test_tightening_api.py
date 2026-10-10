"""The tightening pass over ninaivu/api/ (and the notifier it reports through).

Each test names the finding it covers. Most are about a request that is
refused: refused completely, with a 400 that says why, rather than a 500 or
a 400 that had already changed something.
"""

from __future__ import annotations

import io
import json
import logging

import pytest


def _raw(client, url, text):
    return client.post(url, data=text, content_type="application/json")


# -- API-01 / API-02: a refused settings request changes nothing ---------------

def test_admin_settings_refused_changes_nothing(app, as_admin):
    cfg = app.config["MV_CONFIG"]
    before = (cfg.watch, cfg.open_browsing)
    response = as_admin.post("/api/admin/settings", json={
        "watch": not cfg.watch, "open_browsing": not cfg.open_browsing,
        "remote_hostname": "not a host"})
    assert response.status_code == 400
    assert (cfg.watch, cfg.open_browsing) == before


def test_admin_settings_still_saves_a_good_request(app, as_admin):
    cfg = app.config["MV_CONFIG"]
    response = as_admin.post("/api/admin/settings", json={
        "watch": True, "remote_hostname": "photos.example.org"})
    assert response.status_code == 200
    assert cfg.watch is True and cfg.remote_hostname == "photos.example.org"


def test_privacy_refused_keeps_the_home_zone(app, as_admin):
    cfg = app.config["MV_CONFIG"]
    cfg.home_lat, cfg.home_lon = 10.0, 20.0
    response = as_admin.post("/api/admin/privacy",
                             json={"clear": True, "strip_location": "bogus"})
    assert response.status_code == 400
    assert (cfg.home_lat, cfg.home_lon) == (10.0, 20.0)


def test_repair_settings_infinity_is_a_400_and_changes_nothing(app, as_admin):
    cfg = app.config["MV_CONFIG"]
    before = (cfg.scrub_every_days, cfg.scrub_repair)
    assert _raw(as_admin, "/api/admin/repair/settings",
                '{"every_days": 1e400}').status_code == 400
    response = as_admin.post("/api/admin/repair/settings",
                             json={"every_days": 3, "automatic": "yes"})
    assert response.status_code == 400
    assert (cfg.scrub_every_days, cfg.scrub_repair) == before


def test_offsite_settings_refused_changes_nothing(app, as_admin):
    cfg = app.config["MV_CONFIG"]
    before = (cfg.offsite_kind, cfg.offsite_folder)
    response = _raw(as_admin, "/api/offsite",
                    json.dumps({"kind": "s3" if before[0] != "s3" else "folder",
                                "folder": "/somewhere/else"})[:-1] + ', "every_hours": 1e400}')
    assert response.status_code == 400
    assert (cfg.offsite_kind, cfg.offsite_folder) == before


# -- API-03, SRV-08, SRV-09, SRV-15: the notifier ------------------------------

def test_the_test_message_goes_out_with_its_event_unticked():
    from ninaivu.utils.notify import Notifier
    sent = []
    notifier = Notifier(webhook_url="https://hooks.example/x", enabled_events=("missing",),
                        transport=lambda *args: sent.append(args))
    result = notifier.test()
    assert result["sent"] is True and sent
    # An ordinary report of an unticked event is still not sent.
    assert notifier.send("integrity", "x")["reason"] == "event not enabled"


class _Captured:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return io.BytesIO(b"")


def test_webhook_json_has_a_real_line_break(monkeypatch):
    from ninaivu.utils import notify
    captured = _Captured()
    monkeypatch.setattr(notify, "_OPENER", captured)
    notifier = notify.Notifier(webhook_url="https://hooks.example/x")
    assert notifier.send("missing", "Sum", "Line detail")["sent"]
    body = json.loads(captured.requests[0].data)
    assert body["text"] == "Ninaivu: Sum\nLine detail"
    assert body["content"] == "Ninaivu: Sum\nLine detail"


def test_webhook_form_format_sends_a_form(monkeypatch):
    from urllib.parse import parse_qs
    from ninaivu.utils import notify
    captured = _Captured()
    monkeypatch.setattr(notify, "_OPENER", captured)
    notifier = notify.Notifier(webhook_url="https://hooks.example/x", webhook_format="form")
    assert notifier.send("missing", "Sum", "Line detail")["sent"]
    request = captured.requests[0]
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    form = parse_qs(request.data.decode())
    assert form["title"] == ["Ninaivu: Sum"] and form["message"] == ["Line detail"]


def test_a_retry_does_not_outlive_new_settings(cfg, monkeypatch):
    from ninaivu.utils import notify
    monkeypatch.setattr(notify, "_shared", None)
    cfg.notify_webhook = "https://old.example/hook"
    old = notify.from_config(cfg)
    calls, waiting = [], []

    def failing(*args):
        calls.append(args)
        raise OSError("down")

    old.transport = failing
    old.later = lambda delay, work: waiting.append(work) or object()
    old.send("missing", "Drive gone")
    assert len(calls) == 1 and len(waiting) == 1

    cfg.notify_webhook = "https://new.example/hook"
    assert notify.from_config(cfg) is not old
    waiting[0]()                        # the retry fires after the change
    assert len(calls) == 1, "the old address was posted to again"


# -- API-04: an upload that fails on the server ---------------------------------

def test_upload_failure_is_logged_and_hides_server_paths(app, as_family, monkeypatch, caplog):
    from ninaivu.media import upload_review

    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device", "/secret/state/pending-uploads/x.jpg")

    monkeypatch.setattr(upload_review, "stage", broken)
    with caplog.at_level(logging.ERROR):
        response = as_family.post("/api/upload", data={
            "files": (io.BytesIO(b"\xff\xd8\xff"), "holiday.jpg")},
            content_type="multipart/form-data")
    assert response.status_code == 200
    errors = response.get_json()["errors"]
    assert errors and "/secret" not in json.dumps(errors)
    assert any("not staged" in r.getMessage() for r in caplog.records)


def test_upload_refusal_is_still_said_to_the_person(app, as_family, monkeypatch):
    from ninaivu.media import upload_review

    def refused(*args, **kwargs):
        raise ValueError("This file is not recognised as supported media.")

    monkeypatch.setattr(upload_review, "stage", refused)
    response = as_family.post("/api/upload", data={
        "files": (io.BytesIO(b"x"), "holiday.jpg")}, content_type="multipart/form-data")
    assert response.get_json()["errors"][0]["error"].startswith("This file is not recognised")


# -- API-05: renaming onto another album's name ---------------------------------

def test_album_rename_to_an_existing_name_is_a_409(as_admin):
    first = as_admin.post("/api/albums", json={"name": "Summer"}).get_json()["id"]
    as_admin.post("/api/albums", json={"name": "Winter"})
    response = as_admin.patch(f"/api/albums/{first}", json={"name": "Winter"})
    assert response.status_code == 409
    assert "already exists" in response.get_json()["error"]
    # Its own name, sent back unchanged, is not a clash.
    assert as_admin.patch(f"/api/albums/{first}", json={"name": "Summer"}).status_code == 200


# -- API-06: a recovery file that is not one ------------------------------------

@pytest.mark.parametrize("url", ["/api/cloud/restore/preview", "/api/cloud/restore/start"])
@pytest.mark.parametrize("recovery", [{}, {"key": 5}])
def test_a_bad_recovery_file_is_a_clear_400(as_admin, url, recovery):
    response = as_admin.post(url, json={"recovery": recovery})
    assert response.status_code == 400
    assert "recovery file" in response.get_json()["error"]


# -- API-07: a library folder on a drive that is away ----------------------------

def test_profile_can_be_assigned_to_a_library_that_is_unplugged(app, as_admin, people, tmp_path):
    cfg = app.config["MV_CONFIG"]
    away = str(tmp_path / "unplugged-drive")
    cfg.roots = [*cfg.roots, away]
    response = as_admin.post(f"/api/people/{people['family'].id}",
                             json={"library": f"{away}/kids"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["person"]["library"].endswith("kids")
    # Somewhere that is no library at all is still refused.
    response = as_admin.post(f"/api/people/{people['family'].id}",
                             json={"library": str(tmp_path / "elsewhere")})
    assert response.status_code == 400


# -- API-08: 400s, not 500s ------------------------------------------------------

def test_huge_numbers_and_wrong_bodies_are_not_500s(as_admin):
    assert as_admin.get("/api/copies/single?offset=99999999999999999999").status_code == 200
    assert as_admin.post("/api/import/look", json=[1]).status_code == 400
    assert as_admin.post("/api/import/start", json=[1]).status_code == 400


# -- API-09: the sound-recordings note -------------------------------------------

def test_visibility_note_only_for_sound_recordings(as_admin, people):
    response = as_admin.post("/api/visibility",
                             json={"visibility": "family", "ids": [999991, 999992]})
    assert response.status_code == 200
    assert "note" not in response.get_json()

    conn = people["conn"]
    picture = conn.execute("SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()[0]
    conn.execute("UPDATE assets SET kind='audio', visibility=2 WHERE id=?", (picture,))
    conn.commit()
    response = as_admin.post("/api/visibility", json={"visibility": "family", "ids": [picture]})
    body = response.get_json()
    assert body["kept_back"] == 1 and "Sound recordings" in body["note"]


# -- API-10: a confidence outside 0 to 1 -----------------------------------------

@pytest.mark.parametrize("text", ['{"min_confidence": 80}', '{"min_confidence": NaN}',
                                  '{"min_confidence": -0.5}'])
def test_straighten_confidence_is_held_to_0_to_1(as_admin, text):
    assert _raw(as_admin, "/api/straighten/apply", text).status_code == 400


# -- API-11: face list limits ----------------------------------------------------

def test_face_list_limits_are_clamped(as_admin, monkeypatch):
    from ninaivu.storage import db
    seen = {}

    def listing(*args, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(db, "list_unnamed_clusters", listing)
    as_admin.get("/api/faces/clusters?limit=-1&min_size=-4")
    assert seen["limit"] == 1 and seen["min_size"] == 1
    as_admin.get("/api/faces/clusters?limit=99999999")
    assert seen["limit"] == 1000


def test_face_scan_never_takes_a_negative_batch(app, as_admin, monkeypatch):
    from ninaivu.api import api_faces
    asked = {}

    class Engine:
        available = True

    class Indexer:
        engine = Engine()

        def detect_pass(self, conn, root, *, limit):
            asked["limit"] = limit
            return {"ok": False}

    monkeypatch.setattr(api_faces, "_face_indexer", lambda: Indexer())
    app.config["MV_CONFIG"].faces_enabled = False
    as_admin.post("/api/faces/scan?limit=-1")
    assert "limit" not in asked, "a negative batch ran detection in the request"
    as_admin.post("/api/faces/scan?limit=5")
    assert asked["limit"] == 5


# -- API-14: the sign-in error in the redirect -----------------------------------

def test_cloud_callback_quotes_googles_error(as_admin):
    response = as_admin.get("/api/cloud/callback?error=a%26connected%3D1")
    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].endswith("/#cloud?error=a%26connected%3D1")
    response = as_admin.get("/api/cloud/callback?error=access_denied")
    assert response.headers["Location"].endswith("/#cloud?error=access_denied")
