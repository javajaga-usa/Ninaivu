"""Malformed account requests must fail without changing account state."""

import pytest


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/enter", "/api/me",
                                  "/api/me/password", "/api/people"])
def test_account_endpoints_reject_non_objects(as_admin, path):
    for body in ([], [1], "secret", 12, True, None):
        response = as_admin.post(path, json=body)
        assert response.status_code == 400, (path, body, response.get_json())


def test_setup_rejects_non_objects(client):
    for body in ([1], "secret", 12, True):
        assert client.post("/api/auth/setup", json=body).status_code == 400


def test_update_person_rejects_non_objects(as_admin, people):
    person_id = people["family"].id
    for body in ([1], "secret", 12, True):
        assert as_admin.post(f"/api/people/{person_id}", json=body).status_code == 400


def test_profile_ids_are_positive_sqlite_integers(client):
    for value in (True, 1.5, -1, 0, 2**63, "9" * 100, [], {}, "Infinity"):
        response = client.post("/api/auth/enter", json={"id": value})
        assert response.status_code == 400, (value, response.get_json())
