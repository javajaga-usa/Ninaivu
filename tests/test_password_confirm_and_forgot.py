"""A new password typed twice, and "Forgot password?" on a server with no email.

* The administrator's first profile and "Change password" both ask for the
  new password twice; the server refuses two that differ, so a script or an
  old page cannot slip a typo past the screen. A body without
  ``password_confirm`` is taken at its word, as before.
* "Forgot password?" tells the administrators: the person is marked on the
  console's People page and counted on the Overview until a new password or
  PIN is set for them. The answer never says whether a name is somebody's.
* The Control Panel's "Reset a password…" and ``ninaivu reset-password`` share
  one function, and it clears the mark too.
"""
from __future__ import annotations

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu.api import accounts_api
from ninaivu.server import auth


@pytest.fixture(autouse=True)
def _fresh_allowances():
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()
    yield
    accounts_api._ATTEMPTS.clear()
    accounts_api._LOCKOUTS.clear()


# --- typed twice ------------------------------------------------------------

def test_setup_refuses_two_passwords_that_differ(client, scanned):
    _, conn, _ = scanned
    response = client.post("/api/auth/setup", json={
        "username": "amma", "password": "longenough1", "password_confirm": "longenough2"})
    assert response.status_code == 400
    assert "don't match" in response.get_json()["error"]
    assert auth.needs_setup(conn)


def test_setup_takes_two_that_match(client, scanned):
    _, conn, _ = scanned
    response = client.post("/api/auth/setup", json={
        "username": "amma", "password": "longenough1", "password_confirm": "longenough1"})
    assert response.status_code == 200, response.get_json()
    assert not auth.needs_setup(conn)


def test_setup_without_the_second_box_still_works(client, scanned):
    """Scripts and the --admin start option send one password."""
    response = client.post("/api/auth/setup", json={"username": "amma", "password": "longenough1"})
    assert response.status_code == 200, response.get_json()


def test_changing_a_password_refuses_two_that_differ(app, people):
    me = login(app.test_client(), *FAMILY)
    response = me.post("/api/me/password", json={
        "current": FAMILY[1], "password": "brandnew-99", "password_confirm": "brandnew-98"})
    assert response.status_code == 400
    assert auth.authenticate(people["conn"], FAMILY[0], FAMILY[1]) is not None
    response = me.post("/api/me/password", json={
        "current": FAMILY[1], "password": "brandnew-99", "password_confirm": "brandnew-99"})
    assert response.status_code == 200, response.get_json()
    assert auth.authenticate(people["conn"], FAMILY[0], "brandnew-99") is not None


# --- forgot password --------------------------------------------------------

def _person(as_admin, username):
    return next(p for p in as_admin.get("/api/people").get_json()["people"]
                if p["username"] == username)


def _waiting(as_admin):
    items = as_admin.get("/api/admin/attention").get_json()["items"]
    return next(i for i in items if i["key"] == "signin_help")["count"]


def test_forgot_marks_the_person_for_the_administrators(app, people, as_admin):
    assert _person(as_admin, FAMILY[0])["help_asked"] is None
    assert _waiting(as_admin) == 0

    response = app.test_client().post("/api/auth/forgot", json={"username": FAMILY[0]})
    assert response.status_code == 200
    assert _person(as_admin, FAMILY[0])["help_asked"]
    assert _waiting(as_admin) == 1
    # Nothing about the account itself changed.
    assert auth.authenticate(people["conn"], *FAMILY) is not None


def test_forgot_from_the_picker_takes_the_profile_id(app, people, as_admin):
    app.test_client().post("/api/auth/forgot", json={"id": people["family"].id})
    assert _person(as_admin, FAMILY[0])["help_asked"]


def test_forgot_says_the_same_for_a_name_that_is_nobodys(app, people):
    client = app.test_client()
    real = client.post("/api/auth/forgot", json={"username": FAMILY[0]})
    made_up = client.post("/api/auth/forgot", json={"username": "nobody-here"})
    assert real.status_code == made_up.status_code == 200
    assert real.get_json() == made_up.get_json()


def test_a_reset_from_the_console_clears_the_mark(app, people, as_admin):
    app.test_client().post("/api/auth/forgot", json={"username": FAMILY[0]})
    response = as_admin.post(f"/api/people/{people['family'].id}",
                             json={"password": "temporary-41"})
    assert response.status_code == 200
    assert _person(as_admin, FAMILY[0])["help_asked"] is None
    assert _waiting(as_admin) == 0


def test_a_new_pin_clears_the_mark(app, people, as_admin):
    app.test_client().post("/api/auth/forgot", json={"username": FAMILY[0]})
    as_admin.post(f"/api/people/{people['family'].id}", json={"pin": "4826"})
    assert _person(as_admin, FAMILY[0])["help_asked"] is None


def test_forgot_is_limited(app, people):
    client = app.test_client()
    answers = [client.post("/api/auth/forgot", json={"username": FAMILY[0]}).status_code
               for _ in range(8)]
    assert 429 in answers


def test_a_disabled_profile_is_not_marked(app, people, as_admin):
    auth.set_active(people["conn"], people["family"].id, False)
    app.test_client().post("/api/auth/forgot", json={"username": FAMILY[0]})
    assert _waiting(as_admin) == 0


# --- the Control Panel's reset, and the command's ----------------------------

def test_the_shared_reset_sets_the_password_and_clears_the_mark(app, people):
    from ninaivu.cli import reset_password

    conn = people["conn"]
    auth.ask_for_help(conn, people["admin"].id)
    assert reset_password.reset(conn, ADMIN[0], "forgotten-no-more") == ADMIN[0]
    assert auth.authenticate(conn, ADMIN[0], "forgotten-no-more") is not None
    assert auth.help_asked_at(conn, people["admin"].id) is None


def test_the_shared_reset_names_the_people_when_the_name_is_wrong(people):
    from ninaivu.cli import reset_password

    with pytest.raises(LookupError, match=ADMIN[0]):
        reset_password.reset(people["conn"], "nobody", "forgotten-no-more")
    with pytest.raises(ValueError):
        reset_password.reset(people["conn"], ADMIN[0], "short")


def test_the_sign_in_screens_ask_twice_and_offer_help():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "ninaivu" / "static" / "js"
              / "accounts.js").read_text(encoding="utf-8")
    assert "password_confirm" in source
    assert "Forgot your password?" in source
    assert "accountsApi.forgot" in source or "/api/auth/forgot" in source
