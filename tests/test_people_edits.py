"""Editing a profile from the console: all of a change, or none of it.

The edit endpoint used to write each part of a request as it came to it, so a
role change sent with a PIN that was too short was saved and then answered
with 400 — the console said the change failed and half of it had not. And a
profile with no password could be made an administrator, which left an
administrator nobody could ever sign in as, since the console only takes a
password.
"""

from conftest import ADMIN
from ninaivu.server import auth


def _role(people, user_id):
    return auth.get_user(people["conn"], user_id).role


def _tap_to_enter(people, username="kid"):
    return auth.create_user(people["conn"], username, "", display_name="Kid",
                            role=auth.ROLE_FAMILY, created_by=people["admin"].id)


def test_a_refused_pin_leaves_the_role_alone(as_admin, people):
    guest = people["guest"]
    response = as_admin.post(f"/api/people/{guest.id}",
                             json={"role": "family", "pin": "12"})
    assert response.status_code == 400
    assert _role(people, guest.id) == auth.ROLE_GUEST


def test_a_refused_password_leaves_the_name_alone(as_admin, people):
    guest = people["guest"]
    response = as_admin.post(f"/api/people/{guest.id}",
                             json={"name": "Renamed", "password": "short"})
    assert response.status_code == 400
    assert auth.get_user(people["conn"], guest.id).display_name == "Neighbour"


def test_a_refused_disable_leaves_the_role_alone(as_admin, people):
    """Disabling yourself is refused; the role sent with it must not land."""
    admin = people["admin"]
    other = auth.create_user(people["conn"], "mum", "anotherpass42", role=auth.ROLE_ADMIN)
    response = as_admin.post(f"/api/people/{admin.id}",
                             json={"role": "family", "active": False})
    assert response.status_code == 409
    assert _role(people, admin.id) == auth.ROLE_ADMIN
    assert _role(people, other.id) == auth.ROLE_ADMIN


def test_a_profile_without_a_password_is_not_made_an_admin_alone(as_admin, people):
    kid = _tap_to_enter(people)
    response = as_admin.post(f"/api/people/{kid.id}", json={"role": "admin"})
    assert response.status_code == 400
    assert "password" in response.get_json()["error"]
    assert _role(people, kid.id) == auth.ROLE_FAMILY


def test_promoting_with_a_password_gives_an_admin_who_can_sign_in(as_admin, people):
    kid = _tap_to_enter(people)
    response = as_admin.post(f"/api/people/{kid.id}",
                             json={"role": "admin", "password": "lanternriver77"})
    assert response.status_code == 200, response.get_json()
    fresh = auth.get_user(people["conn"], kid.id)
    assert fresh.role == auth.ROLE_ADMIN
    assert fresh.must_change, "a password the admin chose is a temporary one"
    signed_in = auth.authenticate(people["conn"], "kid", "lanternriver77")
    assert signed_in is not None and signed_in.is_admin


def test_a_session_made_with_a_tap_does_not_become_an_admin_session(as_admin, people):
    """A family member who already has a password can be promoted without a
    new one, but whatever signed them in before — a tap, a PIN — is not what
    an administrator signs in with."""
    family = people["family"]
    conn = people["conn"]
    token, _ = auth.start_session(conn, family.id, "phone")

    response = as_admin.post(f"/api/people/{family.id}", json={"role": "admin"})
    assert response.status_code == 200, response.get_json()
    assert _role(people, family.id) == auth.ROLE_ADMIN
    assert auth.session_user(conn, token) is None


def test_a_disabled_admin_can_be_demoted_while_another_admin_remains(as_admin, people):
    conn = people["conn"]
    retired = auth.create_user(conn, "grandpa", "oldtimerpass8", role=auth.ROLE_ADMIN)
    auth.set_active(conn, retired.id, False)
    assert auth.count_active_admins(conn) == 1

    response = as_admin.post(f"/api/people/{retired.id}", json={"role": "family"})
    assert response.status_code == 200, response.get_json()
    assert _role(people, retired.id) == auth.ROLE_FAMILY


def test_the_last_admin_still_cannot_be_demoted(as_admin, people):
    admin = people["admin"]
    response = as_admin.post(f"/api/people/{admin.id}", json={"role": "family"})
    assert response.status_code == 409
    assert _role(people, admin.id) == auth.ROLE_ADMIN


def test_a_role_change_is_in_the_audit_log(as_admin, people):
    guest = people["guest"]
    assert as_admin.post(f"/api/people/{guest.id}",
                         json={"role": "family"}).status_code == 200
    entries = as_admin.get("/api/audit").get_json()["entries"]
    assert any(e["action"] == "change_role" and "neighbour" in e["detail"]
               for e in entries), entries


def test_sending_the_same_role_again_changes_nothing(as_admin, people):
    guest = people["guest"]
    assert as_admin.post(f"/api/people/{guest.id}",
                         json={"role": "guest"}).status_code == 200
    entries = as_admin.get("/api/audit").get_json()["entries"]
    assert not any(e["action"] == "change_role" for e in entries)


def test_the_signed_in_admin_is_still_signed_in(as_admin, people):
    """Sanity: nothing above signs the acting admin out."""
    guest = people["guest"]
    as_admin.post(f"/api/people/{guest.id}", json={"role": "family"})
    assert as_admin.get("/api/me").get_json()["username"] == ADMIN[0]
