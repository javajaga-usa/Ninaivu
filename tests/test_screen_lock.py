"""The screen locks after a spell with nobody there, and nothing stops.

What is being protected: a signed-in gallery or console left open on a shared
machine. What must not happen: the lock ending the session, or stopping the
work the page was doing — a phone backup keeps going behind a locked screen.
And the lock must hold on the server, not only as an overlay in the page.
"""

import time

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.server import auth

IDLE = 15 * 60


def age(conn, seconds=IDLE + 60):
    """Make every session look as though nobody has been there for a while."""
    conn.execute("UPDATE sessions SET active_at=?", (time.time() - seconds,))
    conn.commit()


@pytest.fixture()
def family(app, people):
    return login(app.test_client(), *FAMILY)


@pytest.fixture()
def apps(scanned, people):
    """Both faces over one set of services, as in production."""
    from ninaivu import build_services, create_admin_app, create_home_app

    cfg, _, _ = scanned
    services = build_services(cfg)
    services.scanner.stop()
    yield {"home": create_home_app(services), "admin": create_admin_app(services)}
    services.stop(timeout=5.0)


def test_a_fresh_sign_in_is_open(family):
    state = family.get("/api/auth/session").get_json()
    assert state["signed_in"] and not state["locked"]
    assert 0 < state["locks_in"] <= IDLE
    assert family.get("/api/assets?limit=1").status_code == 200


def test_fifteen_idle_minutes_lock_it(family, people):
    age(people["conn"])
    refused = family.get("/api/assets?limit=1")
    assert refused.status_code == 423 and refused.get_json()["locked"]
    assert family.get("/api/auth/session").get_json()["locked"]


def test_locked_is_not_signed_out(family, people):
    """Ending the session would stop the page's work; locking must not."""
    age(people["conn"])
    family.get("/api/assets?limit=1")
    assert people["conn"].execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1
    state = family.get("/api/auth/state").get_json()
    assert state["signed_in"] and state["lock"]["locked"]


def test_work_in_progress_carries_on_behind_the_lock(family, people):
    age(people["conn"])
    assert family.get("/api/assets?limit=1").status_code == 423
    # A phone backup the page was sending, and the status line, still answer.
    started = family.post("/api/phone-backup/files", json={
        "device_id": "phone-lock-0001", "name": "IMG_1.png", "size": 10,
        "modified": 1_500_000_000_000})
    assert started.status_code == 200, started.get_json()
    assert family.get("/api/status/activity").status_code == 200
    assert family.get("/api/me").status_code == 200


def test_nothing_that_shows_or_changes_the_library_answers(family, people):
    ids = [r["id"] for r in people["conn"].execute("SELECT id FROM assets LIMIT 1")]
    age(people["conn"])
    for path in (f"/api/thumb/{ids[0]}", f"/api/file/{ids[0]}", "/api/segments",
                 "/api/albums", "/api/facets"):
        assert family.get(path).status_code == 423, path
    assert family.post(f"/api/asset/{ids[0]}", json={"favorite": True}).status_code == 423
    assert family.post("/api/me", json={"display_name": "x"}).status_code == 423


def test_somebody_there_keeps_it_open(family, people):
    age(people["conn"], IDLE - 60)
    assert family.post("/api/auth/active").status_code == 200
    age(people["conn"], 0)                  # just seen
    assert family.get("/api/assets?limit=1").status_code == 200


def test_polling_does_not_count_as_somebody_there(family, people):
    age(people["conn"], IDLE - 30)
    before = people["conn"].execute("SELECT active_at FROM sessions").fetchone()[0]
    family.get("/api/status/activity")
    family.get("/api/auth/session")
    after = people["conn"].execute("SELECT active_at FROM sessions").fetchone()[0]
    assert after == before


def test_being_busy_does_not_unlock_a_locked_screen(family, people):
    age(people["conn"])
    family.get("/api/auth/session")
    assert family.post("/api/auth/active").status_code == 423
    assert family.get("/api/assets?limit=1").status_code == 423


def test_a_tap_to_enter_profile_unlocks_with_a_tap(family, people):
    """It never had a secret; asking for one would lock them out for good."""
    age(people["conn"])
    state = family.get("/api/auth/session").get_json()
    assert state["unlock_with"] == "password"      # FAMILY signs in with one
    auth.set_pin(people["conn"], people["family"].id, None)
    people["conn"].execute("UPDATE users SET password=NULL WHERE id=?",
                           (people["family"].id,))
    people["conn"].commit()
    assert family.get("/api/auth/session").get_json()["unlock_with"] == "none"
    assert family.post("/api/auth/unlock", json={}).status_code == 200
    assert family.get("/api/assets?limit=1").status_code == 200


def test_a_pin_profile_unlocks_with_its_pin(family, people):
    auth.set_pin(people["conn"], people["family"].id, "4321")
    age(people["conn"])
    assert family.get("/api/auth/session").get_json()["unlock_with"] == "pin"
    wrong = family.post("/api/auth/unlock", json={"secret": "0000"})
    assert wrong.status_code == 403 and "PIN" in wrong.get_json()["error"]
    assert family.post("/api/auth/unlock", json={"secret": "4321"}).status_code == 200
    assert family.get("/api/assets?limit=1").status_code == 200


def test_a_password_unlocks_and_a_wrong_one_does_not(family, people):
    age(people["conn"])
    assert family.post("/api/auth/unlock", json={"secret": "nope"}).status_code == 403
    assert family.get("/api/assets?limit=1").status_code == 423
    assert family.post("/api/auth/unlock",
                       json={"secret": FAMILY[1]}).status_code == 200
    assert family.get("/api/assets?limit=1").status_code == 200


def test_too_many_wrong_answers_sign_the_session_out(family, people):
    age(people["conn"])
    for _ in range(7):
        assert family.post("/api/auth/unlock", json={"secret": "nope"}).status_code == 403
    last = family.post("/api/auth/unlock", json={"secret": "nope"})
    assert last.status_code == 401
    assert not family.get("/api/auth/session").get_json()["signed_in"]


def test_the_page_can_lock_it_at_once(family):
    assert family.post("/api/auth/lock").get_json()["locked"]
    assert family.get("/api/assets?limit=1").status_code == 423


def test_the_console_locks_too(apps, people):
    admin = login(apps["admin"].test_client(), *ADMIN)
    assert admin.get("/api/people").status_code == 200
    age(people["conn"])
    assert admin.get("/api/people").status_code == 423
    assert admin.get("/api/auth/session").get_json()["unlock_with"] == "password"
    assert admin.post("/api/auth/unlock", json={"secret": ADMIN[1]}).status_code == 200
    assert admin.get("/api/people").status_code == 200


def test_locking_one_app_does_not_lock_the_other(apps, people):
    """Separate sessions: the console left idle does not cover the gallery."""
    admin = login(apps["admin"].test_client(), *ADMIN)
    guest = login(apps["home"].test_client(), *GUEST)
    admin.post("/api/auth/lock")
    assert admin.get("/api/people").status_code == 423
    assert guest.get("/api/assets?limit=1").status_code == 200


def test_a_session_from_before_the_lock_is_not_locked_on_upgrade(family, people):
    people["conn"].execute("UPDATE sessions SET active_at=0")
    people["conn"].commit()
    assert family.get("/api/assets?limit=1").status_code == 200


def test_zero_minutes_turns_the_lock_off(app, family, people):
    app.config["MV_CONFIG"].lock_after_minutes = 0
    age(people["conn"], 10 * IDLE)
    assert family.get("/api/assets?limit=1").status_code == 200


def test_nobody_signed_in_has_nothing_to_lock(app, people):
    anonymous = app.test_client()
    state = anonymous.get("/api/auth/session").get_json()
    assert not state["signed_in"] and not state["locked"]
    assert anonymous.post("/api/auth/unlock", json={}).status_code == 401
