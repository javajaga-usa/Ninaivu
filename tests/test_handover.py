"""The handover plan, and the door it gives a successor (storage/handover.py,
api/api_handover.py).

What is being protected, roughly in order of how much it would hurt:

* the door opens only for somebody named in the plan, signed in as
  themselves, with their own password and the current code;
* it never opens at once (unless the administrator chose 0 days), every
  administrator can see it waiting and close it, and closing it closes it;
* the plan and the sheet never carry a key or a secret, and refuse text that
  looks like the key was pasted in;
* the plan is the administrator's alone to read and change.
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu.api import accounts_api, api_handover
from ninaivu.server import auth
from ninaivu.storage import handover

SAM = ("sam", "sampassword9")
DAY = 86400


@pytest.fixture()
def household(app, people):
    """Dad (admin), Maya (family, the successor), Sam (family, not named)."""
    conn = people["conn"]
    sam = auth.create_user(conn, SAM[0], SAM[1], display_name="Sam",
                           role=auth.ROLE_FAMILY, created_by=people["admin"].id)
    return {**people, "sam": sam}


@pytest.fixture()
def admin(app, household):
    return login(app.test_client(), *ADMIN)


@pytest.fixture()
def maya(app, household):
    return login(app.test_client(), *FAMILY)


@pytest.fixture()
def sam(app, household):
    return login(app.test_client(), *SAM)


def save(client, household, **changes):
    plan = {"message": "Look after the photographs.", "backups_where": "The grey disk upstairs.",
            "secrets_where": "The blue folder in the steel almirah.",
            "password_where": "Ask Amma; it is in her diary.",
            "machine_access": "The computer under the stairs.", "hardware": "A Mac mini",
            "successors": [household["family"].id],
            "helpers": [{"name": "Ravi", "phone": "+91 98400 00000"}]}
    wait = changes.pop("wait_days", 7)
    plan.update(changes)
    return client.post("/api/admin/handover", json={"plan": plan, "wait_days": wait})


def make_code(client) -> str:
    response = client.post("/api/admin/handover/code")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["code"]


def actions(conn) -> list[str]:
    return [r["action"] for r in conn.execute("SELECT action FROM audit ORDER BY id")]


def role_of(conn, user_id) -> str:
    return auth.get_user(conn, user_id).role


def age_claims(conn, days: float) -> None:
    """Move every claim's dates *days* into the past, as if time had passed."""
    conn.execute("UPDATE handover_claims SET requested_at=requested_at-?, due_at=due_at-?",
                 (days * DAY, days * DAY))
    # The profiles are as much older: one made after its claim began is voided.
    conn.execute("UPDATE users SET created_at=created_at-?", (days * DAY,))
    conn.commit()


# -- the plan: who may read and change it -------------------------------------

def test_only_an_administrator_reads_or_saves_the_plan(app, household, admin, maya):
    assert maya.get("/api/admin/handover").status_code == 403
    assert save(maya, household).status_code == 403
    assert maya.post("/api/admin/handover/code").status_code == 403
    assert maya.get("/api/admin/handover/sheet").status_code == 403
    assert app.test_client().get("/api/admin/handover").status_code == 401
    assert admin.get("/api/admin/handover").status_code == 200


def test_the_plan_is_not_on_the_family_app(scanned):
    from ninaivu import build_services, create_home_app
    cfg, _, _ = scanned
    routes = {str(r) for r in create_home_app(build_services(cfg)).url_map.iter_rules()}
    assert "/api/admin/handover" not in routes
    assert "/api/handover/claim" in routes


def test_a_saved_plan_comes_back(household, admin):
    response = save(admin, household)
    assert response.status_code == 200, response.get_json()
    page = admin.get("/api/admin/handover").get_json()
    assert page["plan"]["successors"] == [household["family"].id]
    assert page["plan"]["secrets_where"] == "The blue folder in the steel almirah."
    assert page["plan"]["helpers"] == [{"name": "Ravi", "phone": "+91 98400 00000"}]
    assert page["wait_days"] == 7 and page["reviewed_at"] > 0 and page["reviewed_by"] == "Dad"
    assert [p["name"] for p in page["people"]] == ["Maya", "Sam"]
    assert "handover_plan_saved" in actions(household["conn"])


@pytest.mark.parametrize("who", ["admin", "guest"])
def test_a_successor_is_a_family_member(household, admin, who):
    response = save(admin, household, successors=[household[who].id])
    assert response.status_code == 400


@pytest.mark.parametrize("days", [-1, 31, "7", True])
def test_the_wait_is_zero_to_thirty_days(household, admin, days):
    assert save(admin, household, wait_days=days).status_code == 400


# -- no secrets in, no secrets out ---------------------------------------------

@pytest.fixture()
def secrets_on_this_machine(app, scanned):
    """A real backup key and an off-site store's secret key."""
    from ninaivu.cloud import keyring
    cfg, _, _ = scanned
    record = keyring.create(cfg.state_dir, "our family photographs 2026",
                            "our family photographs 2026")
    services = app.config["MV_SERVICES"]
    services.offsite.save_secret("Zq9secretS3key/ForTheBucket+abc")
    cfg.offsite_enabled = True
    cfg.offsite_kind = "s3"
    cfg.offsite_endpoint = "https://s3.example.com"
    cfg.offsite_bucket = "family"
    cfg.offsite_access_key = "AKIAEXAMPLE"
    cfg.mirror_enabled = True
    cfg.mirror_dir = str(cfg.state_dir.parent / "second-disk")
    key = base64.b64decode(record["key"])
    return {"record": record, "secrets": [record["key"], key.hex(),
                                          "Zq9secretS3key/ForTheBucket+abc"]}


def test_what_ninaivu_fills_in_never_carries_a_secret(household, admin, secrets_on_this_machine):
    save(admin, household)
    page = admin.get("/api/admin/handover")
    sheet = admin.get("/api/admin/handover/sheet?lang=en")
    for text in (page.get_data(as_text=True), sheet.get_data(as_text=True)):
        for secret in secrets_on_this_machine["secrets"]:
            assert secret not in text
    found = page.get_json()["facts"]
    key_id = secrets_on_this_machine["record"]["key_id"].upper()
    assert found["key_fingerprint"].replace(" ", "") == key_id
    assert key_id[:4] in sheet.get_data(as_text=True)
    kinds = {d["kind"] for d in found["destinations"]}
    assert kinds == {"index", "mirror", "offsite"}
    offsite = next(d for d in found["destinations"] if d["kind"] == "offsite")
    assert offsite["where"] == "https://s3.example.com/family/ninaivu"
    assert found["version"] and found["machine"] and found["state_dir"]
    assert found["restore_steps"]


def test_the_actual_key_is_refused(household, admin, secrets_on_this_machine):
    for secret in secrets_on_this_machine["secrets"]:
        response = save(admin, household, secrets_where=f"It is {secret}")
        assert response.status_code == 400, secret
        assert "where it is kept" in response.get_json()["error"]


@pytest.mark.parametrize("pasted", [
    base64.b64encode(bytes(range(32))).decode(),
    "a3f1c9e2" * 8,
    '{"format": "ninaivu-encrypted-backup", "key": "abc"}',
    "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC2eF4hJ6kL8n",
])
def test_text_that_looks_like_a_key_is_refused(household, admin, pasted):
    for field in ("secrets_where", "password_where", "message"):
        assert save(admin, household, **{field: f"here: {pasted}"}).status_code == 400
    assert save(admin, household, helpers=[{"name": pasted, "phone": ""}]).status_code == 400


@pytest.mark.parametrize("where", [
    "The blue folder in the steel almirah, second shelf.",
    "/Volumes/PhotoBackup2/ninaivu-offsite",
    r"D:\Backups\Family Photos 2024",
    "https://s3.eu-central-003.backblazeb2.com/familyphotos2024/ninaivu",
])
def test_saying_where_things_are_is_welcome(household, admin, where):
    assert save(admin, household, secrets_where=where, backups_where=where).status_code == 200


# -- the sheet ------------------------------------------------------------------

def test_the_sheet_prints_in_english(household, admin):
    save(admin, household)
    response = admin.get("/api/admin/handover/sheet?lang=en")
    assert response.status_code == 200 and response.mimetype == "text/html"
    assert response.headers["Cache-Control"] == "no-store"
    page = response.get_data(as_text=True)
    assert "If something happens to me — our family photos" in page
    assert "The blue folder in the steel almirah." in page
    assert "Maya" in page and "Ravi" in page
    assert "@page" in page and "A4" in page
    assert not re.search(r"\{[a-z_]+\}", page), "a placeholder was left unfilled"
    assert "<script>" not in page, "the policy allows no inline script"


def test_the_sheet_prints_in_tamil(household, admin):
    save(admin, household)
    page = admin.get("/api/admin/handover/sheet?lang=ta").get_data(as_text=True)
    ta = json.loads((api_handover.LOCALES / "ta.json").read_text(encoding="utf-8"))
    assert ta["If something happens to me — our family photos"] in page
    assert ta["To bring the photographs back"] in page
    assert '<html lang="ta">' in page
    assert not re.search(r"\{[a-z_]+\}", page)
    # The administrator's own words stay as they were written.
    assert "The blue folder in the steel almirah." in page


def test_the_sheet_escapes_what_was_typed(household, admin):
    save(admin, household, message="<script>alert(1)</script>")
    page = admin.get("/api/admin/handover/sheet").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_sheet_carries_the_code_only_when_it_is_the_current_one(household, admin):
    save(admin, household)
    code = make_code(admin)
    printed = admin.post("/api/admin/handover/sheet", data={"code": code, "lang": "en"})
    assert code in printed.get_data(as_text=True)
    forged = admin.post("/api/admin/handover/sheet", data={"code": "ABCD-EFGH-JKMN-PQRS"})
    assert "ABCD-EFGH-JKMN-PQRS" not in forged.get_data(as_text=True)


# -- the code -------------------------------------------------------------------

def test_the_code_is_kept_only_as_a_hash(household, admin):
    code = make_code(admin)
    assert re.fullmatch(r"([A-HJ-NP-TW-Z2-9]{4}-){3}[A-HJ-NP-TW-Z2-9]{4}", code)
    stored = household["conn"].execute("SELECT code_hash FROM handover_plan").fetchone()[0]
    assert stored.startswith("scrypt$")
    assert handover.normalise_code(code) not in stored and code not in stored
    page = admin.get("/api/admin/handover").get_data(as_text=True)
    assert code not in page and stored not in page
    assert admin.get("/api/admin/handover").get_json()["has_code"] is True
    assert "handover_code_made" in actions(household["conn"])


def test_the_code_works_once(household, admin, maya):
    save(admin, household)
    code = make_code(admin)
    first = maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    assert first.status_code == 200, first.get_json()
    claim = handover.waiting(household["conn"])[0]
    assert admin.post(f"/api/handover/claims/{claim['id']}/cancel").status_code == 200
    again = maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    assert again.status_code == 403
    assert admin.get("/api/admin/handover").get_json()["has_code"] is False


def test_a_new_code_ends_the_old_one(household, admin, maya):
    save(admin, household)
    old = make_code(admin)
    new = make_code(admin)
    assert old != new
    assert maya.post("/api/handover/claim",
                     json={"code": old, "password": FAMILY[1]}).status_code == 403
    # Typed the way it is read aloud: lower case, spaces for dashes.
    typed = new.lower().replace("-", " ")
    assert maya.post("/api/handover/claim",
                     json={"code": typed, "password": FAMILY[1]}).status_code == 200


# -- who may use it ---------------------------------------------------------------

def test_somebody_not_named_cannot_use_the_right_code(household, admin, sam, maya):
    save(admin, household)
    code = make_code(admin)
    response = sam.post("/api/handover/claim", json={"code": code, "password": SAM[1]})
    assert response.status_code == 403
    assert role_of(household["conn"], household["sam"].id) == auth.ROLE_FAMILY
    assert "handover_refused" in actions(household["conn"])
    # A stolen code tried by somebody else does not use it up.
    assert maya.post("/api/handover/claim",
                     json={"code": code, "password": FAMILY[1]}).status_code == 200


def test_a_guest_cannot_even_ask(app, household, admin):
    save(admin, household)
    code = make_code(admin)
    guest = login(app.test_client(), "neighbour", "visitingpass9")
    assert guest.post("/api/handover/claim", json={"code": code}).status_code == 403
    assert app.test_client().post("/api/handover/claim", json={"code": code}).status_code == 401


def test_the_successor_gives_their_own_password(household, admin, maya):
    save(admin, household)
    code = make_code(admin)
    wrong = maya.post("/api/handover/claim", json={"code": code, "password": "not-hers-at-all"})
    assert wrong.status_code == 403
    assert not handover.waiting(household["conn"])
    assert admin.get("/api/admin/handover").get_json()["has_code"] is True


def test_a_successor_with_a_pin_gives_it_and_chooses_a_password(app, household, admin):
    """A profile that opens with a PIN proves itself with the PIN; the password
    an administrator needs is chosen then, and kept only once the claim is made.
    Choosing it *was* the whole proof once (see test_audit_2026_10_10_auth)."""
    conn = household["conn"]
    conn.execute("UPDATE users SET password=NULL WHERE id=?", (household["family"].id,))
    conn.commit()
    auth.set_pin(conn, household["family"].id, "4927")
    save(admin, household, wait_days=0)
    code = make_code(admin)
    maya = app.test_client()
    token, _ = auth.start_session(conn, household["family"].id, "test", face="home")
    maya.set_cookie(auth.SESSION_COOKIE, token)
    assert maya.post("/api/handover/claim", json={"code": code, "pin": "4927", "password": "short"}
                     ).status_code == 400
    assert maya.post("/api/handover/claim",
                     json={"code": code, "pin": "4927", "password": "a-new-password-9"}
                     ).status_code == 200
    assert role_of(conn, household["family"].id) == auth.ROLE_ADMIN
    assert auth.authenticate(conn, FAMILY[0], "a-new-password-9") is not None


def test_guesses_are_limited(household, admin, maya):
    save(admin, household)
    code = make_code(admin)
    for _ in range(5):
        assert maya.post("/api/handover/claim",
                         json={"code": "AAAA-BBBB-CCCC-DDDD", "password": FAMILY[1]}
                         ).status_code == 403
    blocked = maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    assert blocked.status_code == 429
    assert actions(household["conn"]).count("handover_code_wrong") == 5
    assert "handover_limited" in actions(household["conn"])


def test_somebody_not_named_is_limited_too(household, admin, sam):
    save(admin, household)
    make_code(admin)
    codes = [sam.post("/api/handover/claim", json={"code": "X", "password": SAM[1]}).status_code
             for _ in range(6)]
    assert codes == [403] * 5 + [429]


# -- the wait -------------------------------------------------------------------

def test_nothing_happens_until_the_wait_is_over(household, admin, maya):
    conn = household["conn"]
    save(admin, household, wait_days=7)
    code = make_code(admin)
    response = maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    assert response.status_code == 200 and response.get_json()["done"] is False
    claim = response.get_json()["claim"]
    assert round((claim["due_at"] - claim["requested_at"]) / DAY) == 7
    assert role_of(conn, household["family"].id) == auth.ROLE_FAMILY

    # Every administrator is told.
    waiting = admin.get("/api/handover/claims").get_json()["waiting"]
    assert [w["name"] for w in waiting] == ["Maya"]
    assert maya.get("/api/handover/claims").status_code == 403

    age_claims(conn, 6)
    assert maya.get("/api/handover/me").get_json()["claim"]["state"] == "waiting"
    assert role_of(conn, household["family"].id) == auth.ROLE_FAMILY

    age_claims(conn, 1.01)
    assert maya.get("/api/handover/me").status_code in (200, 401)
    assert role_of(conn, household["family"].id) == auth.ROLE_ADMIN
    # Nobody else changed: the administrator who was here still is.
    assert role_of(conn, household["admin"].id) == auth.ROLE_ADMIN
    assert role_of(conn, household["sam"].id) == auth.ROLE_FAMILY
    assert "handover_complete" in actions(conn)
    assert not handover.waiting(conn)
    # Signed in as a family member before: an administrator signs in again.
    assert maya.get("/api/me").get_json().get("role") != auth.ROLE_ADMIN


def test_the_successor_cannot_skip_the_wait(household, admin, maya):
    conn = household["conn"]
    save(admin, household, wait_days=30)
    code = make_code(admin)
    maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    # Not by shortening it, not by asking again, not by cancelling and retrying.
    assert save(maya, household, wait_days=0).status_code == 403
    assert maya.post("/api/handover/claim",
                     json={"code": code, "password": FAMILY[1]}).status_code == 409
    claim = handover.waiting(conn)[0]
    assert maya.post(f"/api/handover/claims/{claim['id']}/cancel").status_code == 403
    # The administrator shortening it later does not shorten a claim made.
    save(admin, household, wait_days=0)
    assert api_handover.complete_due(conn) == 0
    assert role_of(conn, household["family"].id) == auth.ROLE_FAMILY


def test_the_next_request_after_the_deadline_finishes_it(app, household, admin, maya):
    conn = household["conn"]
    save(admin, household)
    maya.post("/api/handover/claim", json={"code": make_code(admin), "password": FAMILY[1]})
    age_claims(conn, 8)
    api_handover._CHECKED.clear()
    # Any request at all, by anybody: a guest's look at the gallery will do.
    app.test_client().get("/api/auth/state")
    assert role_of(conn, household["family"].id) == auth.ROLE_ADMIN


def test_cancelling_ends_that_claim(household, admin, maya):
    conn = household["conn"]
    save(admin, household)
    code = make_code(admin)
    maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    claim = handover.waiting(conn)[0]
    assert admin.post(f"/api/handover/claims/{claim['id']}/cancel").status_code == 200
    assert admin.post(f"/api/handover/claims/{claim['id']}/cancel").status_code == 409
    age_claims(conn, 30)
    assert api_handover.complete_due(conn) == 0
    assert role_of(conn, household["family"].id) == auth.ROLE_FAMILY
    assert maya.get("/api/handover/me").get_json()["claim"]["state"] == "cancelled"
    assert "handover_cancelled" in actions(conn)


def test_taken_off_the_list_while_waiting_changes_nothing(household, admin, maya):
    conn = household["conn"]
    save(admin, household)
    maya.post("/api/handover/claim", json={"code": make_code(admin), "password": FAMILY[1]})
    save(admin, household, successors=[household["sam"].id])
    age_claims(conn, 8)
    assert api_handover.complete_due(conn) == 0
    assert role_of(conn, household["family"].id) == auth.ROLE_FAMILY
    assert "handover_void" in actions(conn)


def test_with_no_wait_it_is_at_once(household, admin, maya):
    conn = household["conn"]
    save(admin, household, wait_days=0)
    response = maya.post("/api/handover/claim",
                         json={"code": make_code(admin), "password": FAMILY[1]})
    assert response.status_code == 200 and response.get_json()["done"] is True
    assert role_of(conn, household["family"].id) == auth.ROLE_ADMIN
    assert role_of(conn, household["admin"].id) == auth.ROLE_ADMIN


def test_everything_is_in_the_activity_log(household, admin, maya, sam):
    conn = household["conn"]
    save(admin, household, wait_days=0)
    code = make_code(admin)
    sam.post("/api/handover/claim", json={"code": code, "password": SAM[1]})
    maya.post("/api/handover/claim", json={"code": "WRONG", "password": FAMILY[1]})
    maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    logged = actions(conn)
    for action in ("handover_plan_saved", "handover_code_made", "handover_refused",
                   "handover_code_wrong", "handover_requested", "handover_complete"):
        assert action in logged, action
    details = " ".join(r[0] for r in conn.execute("SELECT detail FROM audit"))
    assert handover.normalise_code(code) not in details.replace("-", "")
    assert admin.get("/api/audit").status_code == 200


def test_the_successor_sees_the_entry_and_nobody_else_does(household, admin, maya, sam):
    assert maya.get("/api/handover/me").get_json()["successor"] is False
    save(admin, household)
    assert maya.get("/api/handover/me").get_json()["successor"] is True
    assert sam.get("/api/handover/me").get_json()["successor"] is False
    assert admin.get("/api/handover/me").get_json()["successor"] is False


# -- the nudge --------------------------------------------------------------------

def nudges_of(client) -> list[str]:
    items = client.get("/api/admin/attention").get_json()["items"]
    item = next(i for i in items if i["key"] == "handover")
    assert item["page"] == "handover"
    assert (item["count"] == 1) == bool(item["nudges"])
    return item["nudges"]


def test_no_plan_is_a_nudge(household, admin):
    assert nudges_of(admin) == ["none"]
    save(admin, household)
    assert nudges_of(admin) == []


def test_an_old_plan_is_a_nudge(household, admin):
    save(admin, household)
    conn = household["conn"]
    conn.execute("UPDATE handover_plan SET reviewed_at=reviewed_at-?", (190 * DAY,))
    conn.commit()
    assert nudges_of(admin) == ["old"]
    save(admin, household)
    assert nudges_of(admin) == []


def test_changed_backups_are_a_nudge(app, household, admin, scanned):
    save(admin, household)
    cfg, _, _ = scanned
    cfg.mirror_enabled = True
    cfg.mirror_dir = str(cfg.state_dir.parent / "second-disk")
    assert nudges_of(admin) == ["backups"]
    save(admin, household)
    assert nudges_of(admin) == []


def test_the_nudge_rules():
    record = {"reviewed_at": 1000.0, "destinations": "abc"}
    assert handover.nudges({"reviewed_at": 0}, "abc", now=2000) == ["none"]
    assert handover.nudges(record, "abc", now=2000) == []
    assert handover.nudges(record, "abc", now=1000 + 183 * DAY) == ["old"]
    assert handover.nudges(record, "xyz", now=2000) == ["backups"]
    one = handover.destinations_fingerprint([{"kind": "mirror", "where": "/a", "last_ok": 1}])
    two = handover.destinations_fingerprint([{"kind": "mirror", "where": "/a", "last_ok": 9}])
    assert one == two, "a backup that ran again is the same backup"


@pytest.fixture(autouse=True)
def _checks_start_afresh():
    api_handover._CHECKED.clear()
    accounts_api._ATTEMPTS.clear()
    yield
    api_handover._CHECKED.clear()
