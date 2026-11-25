"""Odd input sent to a write route is told what was wrong, never answered 500.

A sweep in November 2026 sent every POST, PATCH and DELETE route, as a family
member and as an administrator, bodies with each key it reads set to a word, a
negative number, a number past 64 bits, null, a list and an object, and every
GET route its query arguments the same way. These are what it found.
"""
from __future__ import annotations

import pytest

from conftest import ADMIN, login

HUGE = 10 ** 20


@pytest.fixture()
def admin(app, people):
    return login(app.test_client(), *ADMIN)


@pytest.mark.parametrize("path", [
    "/api/visibility/undo", "/api/straighten/undo", "/api/duplicates/review/put-back",
    "/api/faces/confirm", "/api/faces/reject",
])
def test_a_number_past_64_bits_is_a_400(admin, path):
    body = {"batch": HUGE, "batch_id": HUGE, "face_id": HUGE, "person_id": HUGE, "id": HUGE}
    response = admin.post(path, json=body)
    assert response.status_code == 400, response.get_json()
    assert response.get_json()["error"] == "That number is too large."


def test_forgot_password_with_a_huge_id_is_a_400(app, people):
    response = app.test_client().post("/api/auth/forgot", json={"id": HUGE})
    assert response.status_code in (400, 404), response.get_json()


@pytest.mark.parametrize("path", ["/api/archive/validate", "/api/archive/capacity",
                                  "/api/archive/start"])
@pytest.mark.parametrize("body", [{"destination_dir": -1}, {"destination_dir": ["x"]},
                                  {"source_dirs": -1}, {"source_dirs": "x", "media_types": 5}])
def test_import_with_the_wrong_kind_of_value_says_so(admin, path, body):
    response = admin.post(path, json=body)
    assert response.status_code == 400, response.get_json()


def test_a_person_named_with_a_photograph_no_longer_there(admin):
    response = admin.post("/api/faces/person", json={"name": "Paati", "avatar_asset_id": 999999})
    assert response.status_code == 400
    assert response.get_json()["error"] == "That photograph is no longer in the library."
    assert admin.post("/api/faces/person", json={"name": ["Paati"]}).status_code == 400
    made = admin.post("/api/faces/person", json={"name": "Paati"})
    assert made.status_code == 200 and made.get_json()["person"]["name"] == "Paati"


@pytest.mark.parametrize("path", ["/api/admin/folder?offset=99999999999999999999",
                                  "/api/admin/large-files?min_mb=99999999999999999999",
                                  "/api/library/browse?path=%00"])
def test_odd_query_values_are_not_a_500(admin, path):
    assert admin.get(path).status_code < 500


@pytest.mark.parametrize("path, body", [
    ("/api/library/root", {"path": "/tmp/a\x00b"}),
    ("/api/archive/adopt", {"path": "/tmp/a\x00b"}),
    ("/api/offsite/restore", {"folder": "/tmp/a\x00b"}),
])
def test_a_nul_in_a_folder_name_is_a_400(admin, path, body):
    """Found by the 25 November 2026 sweep: Python refuses a path with a NUL
    in it (ValueError) before any check of the route's own, which was a 500."""
    response = admin.post(path, json=body)
    assert response.status_code == 400, response.get_json()
    assert "character no folder can have" in response.get_json()["error"]


def test_any_other_value_error_is_still_a_fault(app, people):
    """The NUL answer is for that one refusal: other ValueErrors stay faults."""
    from flask import Blueprint
    probe = Blueprint("probe_value_error", __name__)

    @probe.get("/api/probe-value-error")
    def _boom():
        raise ValueError("something else")

    app.register_blueprint(probe)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    client = login(app.test_client(), *ADMIN)
    assert client.get("/api/probe-value-error").status_code == 500
