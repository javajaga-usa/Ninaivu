"""The Overview's "Signed in now" counts people, not sessions.

Every device somebody signs in on keeps a session of its own — the phone, the
tablet in the kitchen, a second browser tab — so the figure read 20 in a house
with four profiles. It is the number of distinct people with a live session.
"""

import time

from conftest import ADMIN, FAMILY, login


def _people(client):
    return client.get("/api/admin/overview").get_json()["people"]


def test_several_devices_of_one_person_count_once(app, people, as_admin):
    # Two more devices for the administrator, two for a family member.
    login(app.test_client(), *ADMIN)
    login(app.test_client(), *ADMIN)
    login(app.test_client(), *FAMILY)
    login(app.test_client(), *FAMILY)

    counts = _people(as_admin)
    assert counts["signed_in"] == 2
    assert counts["sessions"] >= 5
    assert counts["signed_in"] <= counts["total"]


def test_somebody_just_looking_is_not_counted(app, people, as_admin, anon):
    anon.get("/api/assets?limit=1")
    assert _people(as_admin)["signed_in"] == 1


def test_an_expired_session_is_not_somebody_signed_in(app, people, as_admin):
    login(app.test_client(), *FAMILY)
    assert _people(as_admin)["signed_in"] == 2

    conn = people["conn"]
    conn.execute("UPDATE sessions SET expires_at=? WHERE user_id=?",
                 (time.time() - 60, people["family"].id))
    conn.commit()

    assert _people(as_admin)["signed_in"] == 1
