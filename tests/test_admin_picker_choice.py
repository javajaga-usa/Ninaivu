"""An administrator can leave their tile off the family app's sign-in screen.

On by default. Off, the picker says nothing about them — not their name, not
their picture, not their tile's door — and they sign in with their username
and password as before.
"""

from conftest import ADMIN, login


def _picker_ids(client):
    return [p["id"] for p in client.get("/api/auth/profiles").get_json()["profiles"]]


def test_an_administrator_is_on_the_picker_by_default(anon, people):
    assert people["admin"].id in _picker_ids(anon)
    assert anon.get("/api/me").get_json()  # nothing else needed


def test_off_the_picker_they_sign_in_by_username(app, anon, as_admin, people):
    admin_id = people["admin"].id
    body = as_admin.post("/api/me", json={"on_picker": False}).get_json()
    assert body["on_picker"] is False

    visitor = app.test_client()
    assert admin_id not in _picker_ids(visitor)
    assert people["family"].id in _picker_ids(visitor)
    # The tile's door is shut too, and answers as for nobody.
    refused = visitor.post("/api/auth/enter", json={"id": admin_id, "secret": ADMIN[1]})
    assert refused.status_code == 401
    nobody = visitor.post("/api/auth/enter", json={"id": 999999, "secret": ADMIN[1]})
    assert refused.get_json() == nobody.get_json()
    # Their picture is not served to somebody not signed in.
    assert visitor.get(f"/api/avatar/{admin_id}").status_code == 404
    # The username login still works.
    login(app.test_client(), *ADMIN)

    # And back on.
    assert as_admin.post("/api/me", json={"on_picker": True}).get_json()["on_picker"]
    assert admin_id in _picker_ids(app.test_client())


def test_only_an_administrator_chooses(as_family):
    response = as_family.post("/api/me", json={"on_picker": False})
    assert response.status_code == 403


def test_the_console_sets_it_for_an_administrator_only(as_admin, people):
    admin_id, family_id = people["admin"].id, people["family"].id
    ok = as_admin.post(f"/api/people/{admin_id}", json={"on_picker": False})
    assert ok.status_code == 200 and ok.get_json()["person"]["on_picker"] is False
    refused = as_admin.post(f"/api/people/{family_id}", json={"on_picker": False})
    assert refused.status_code == 400
    bad = as_admin.post(f"/api/people/{admin_id}", json={"on_picker": "no"})
    assert bad.status_code == 400
