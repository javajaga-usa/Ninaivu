"""Regressions for the sign-in findings of the 10 October 2026 security audit.

Each test is the weakness the audit demonstrated, run against the fix:

* the session cookie went out without ``Secure`` behind the shipped proxy
  configurations, so the proxy's own HTTP-to-HTTPS redirect carried it in
  the clear (M1, the cookie part);
* a made-up username was paused differently from a real one after the second
  round of guesses, which told a guesser which names exist, and every miss
  with a made-up name was a row in the activity log (L6);
* a successor whose profile had no password proved themselves to the
  handover with whatever password they typed in that moment (L7);
* two setup requests arriving together each made an administrator (L8).
"""
from __future__ import annotations

import threading
import time

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu.api import accounts_api
from ninaivu.server import auth
from ninaivu.storage import db, handover


def cookie_line(response, name: str = auth.SESSION_COOKIE) -> str:
    lines = [h for h in response.headers.getlist("Set-Cookie") if h.startswith(name + "=")]
    assert lines, f"no {name} cookie was set"
    return lines[0]


# --- M1. The session cookie is Secure wherever the name is only ever HTTPS --

def test_the_public_name_gets_a_secure_cookie_over_a_plain_http_proxy_hop(app, people, scanned):
    """nginx and Caddy speak plain HTTP to Ninaivu, and with ``trusted_proxies``
    at 0 the forwarded-proto header is dropped unread: ``request.is_secure`` is
    False, and the cookie used to go out without the flag."""
    cfg, _, _ = scanned
    cfg.remote_hostname = "photos.example.org"
    client = app.test_client()
    response = client.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
                           headers={"Host": "photos.example.org"})
    assert response.status_code == 200
    assert "; Secure" in cookie_line(response)


def test_a_tailscale_name_gets_a_secure_cookie_too(app, people):
    response = app.test_client().post(
        "/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
        headers={"Host": "ninaivu.tail1234.ts.net"})
    assert response.status_code == 200
    assert "; Secure" in cookie_line(response)


@pytest.mark.parametrize("host", ["localhost:5000", "192.168.1.20:5000", "ninaivu.local"])
def test_plain_http_at_home_does_not(app, people, scanned, host):
    """A browser drops a Secure cookie set over HTTP: the flag here would mean
    nobody at home could sign in."""
    cfg, _, _ = scanned
    cfg.remote_hostname = "photos.example.org"
    response = app.test_client().post(
        "/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
        headers={"Host": host})
    assert response.status_code == 200
    assert "; Secure" not in cookie_line(response)


def test_the_picker_and_the_console_make_the_same_decision(scanned):
    from ninaivu import build_services, create_admin_app, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.remote_hostname = "photos.example.org"
    cfg.allowed_hosts = ["admin.photos.example.org"]
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    kid = auth.create_user(conn, "kid", "", display_name="Kid", role=auth.ROLE_FAMILY, pin="4927")
    services = build_services(cfg)
    services.scanner.stop()
    public = {"Host": "photos.example.org"}

    home = create_home_app(services).test_client()
    entered = home.post("/api/auth/enter", json={"id": kid.id, "secret": "4927"}, headers=public)
    assert entered.status_code == 200, entered.get_json()
    assert "; Secure" in cookie_line(entered)

    console = create_admin_app(services).test_client()
    signed = console.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
                          headers={"Host": "admin.photos.example.org"})
    assert signed.status_code == 200
    # The console's own name is not the public name, and over plain HTTP it
    # gets no flag — exactly as the family app would not.
    assert "; Secure" not in cookie_line(signed, auth.ADMIN_SESSION_COOKIE)
    signed = console.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
                          headers=public)
    assert "; Secure" in cookie_line(signed, auth.ADMIN_SESSION_COOKIE)


def test_https_is_secure_whatever_the_name(app, people):
    response = app.test_client().post(
        "/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
        base_url="https://localhost")
    assert "; Secure" in cookie_line(response)


# --- L6. A made-up name is paused exactly like a real one -----------------------

def _misses(client, name: str, n: int) -> list[int]:
    """*n* wrong passwords, from an address that changes every four so the
    per-address allowance never answers first."""
    return [client.post("/api/auth/login", json={"username": name, "password": "wrong"},
                        environ_base={"REMOTE_ADDR": f"2001:db8::{i // 4 + 1:x}"}).status_code
            for i in range(n)]


def test_a_made_up_name_is_paused_like_a_real_one(app, people, monkeypatch):
    """Twenty misses pause either kind of name for half an hour; the second
    twenty used to pause a real name for an hour and a made-up one not at all
    beyond its window, so the moment the 429 lifted told the two apart."""
    clock = {"now": time.time()}
    monkeypatch.setattr(accounts_api.time, "time", lambda: clock["now"])
    client = app.test_client()
    names = (ADMIN[0], "nobody-of-that-name")

    for name in names:
        assert _misses(client, name, 20) == [401] * 20
        assert _misses(client, name, 1) == [429]
    clock["now"] += 31 * 60
    for name in names:
        assert _misses(client, name, 20) == [401] * 20, name
        assert _misses(client, name, 1) == [429]
    clock["now"] += 31 * 60
    # The second pause is an hour: both are still paused.
    assert [_misses(client, name, 1) for name in names] == [[429], [429]]
    clock["now"] += 31 * 60
    assert [_misses(client, name, 1) for name in names] == [[401], [401]]


def test_the_401_reads_the_same_for_both(app, people):
    client = app.test_client()
    real = client.post("/api/auth/login", json={"username": ADMIN[0], "password": "wrong"})
    made_up = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
    assert real.status_code == made_up.status_code == 401
    assert real.get_json() == made_up.get_json()


def test_misses_with_made_up_names_do_not_fill_the_activity_log(app, people):
    """One row per miss for a real name; for names that are nobody's, twenty a
    half hour from everywhere together — still a plain "somebody is guessing"
    for the administrator, no longer a log the internet writes at will."""
    conn = people["conn"]
    client = app.test_client()
    for i in range(25):
        response = client.post("/api/auth/login", json={"username": f"guess{i}", "password": "x"},
                               environ_base={"REMOTE_ADDR": f"2001:db8:1::{i + 1:x}"})
        assert response.status_code == 401
    for i in range(6):
        client.post("/api/auth/login", json={"username": ADMIN[0], "password": "x"},
                    environ_base={"REMOTE_ADDR": f"2001:db8:2::{i + 1:x}"})
    rows = conn.execute("SELECT detail FROM audit WHERE action='login_failed'").fetchall()
    details = [r["detail"] for r in rows]
    assert sum(d.startswith("guess") for d in details) == 20
    assert details.count(ADMIN[0]) == 6


def test_a_pause_long_over_is_forgotten_and_the_table_stays_bounded(app, people):
    """What keeps pausing made-up names from keeping a row for every name the
    internet ever tried: a pause that ended the longest pause ago fades, in
    memory and in the index alike."""
    conn = people["conn"]
    with app.test_request_context("/"):
        now = time.time()
        key = "*|user:long-ago"
        # A pause that ended a moment ago is remembered, in both places.
        accounts_api._LOCKOUTS[key] = (3, now - 100)
        accounts_api._save(key)
        assert conn.execute("SELECT strikes FROM auth_limits WHERE key=?", (key,)).fetchone()[0] == 3
        accounts_api._last_sweep = 0.0
        accounts_api.rate_limited("anything")
        assert key in accounts_api._LOCKOUTS
        # One that ended the longest pause ago is not.
        accounts_api._LOCKOUTS[key] = (3, now - accounts_api._LONGEST_PAUSE - 1)
        accounts_api._last_sweep = 0.0
        accounts_api.rate_limited("anything")
        assert key not in accounts_api._LOCKOUTS
        # The row saved before a long shutdown goes at the next save of any
        # account-wide key, which sweeps the index too.
        conn.execute("UPDATE auth_limits SET until=? WHERE key=?",
                     (now - accounts_api._LONGEST_PAUSE - 1, key))
        conn.commit()
        accounts_api._ATTEMPTS["*|user:other"] = [now]
        accounts_api._save("*|user:other")
        assert conn.execute("SELECT 1 FROM auth_limits WHERE key=?", (key,)).fetchone() is None
        # A pause still running stays whatever else is swept.
        accounts_api._LOCKOUTS["*|user:paused"] = (1, now + 100)
        accounts_api._last_sweep = 0.0
        accounts_api.rate_limited("anything")
        assert "*|user:paused" in accounts_api._LOCKOUTS


def test_the_pause_never_outgrows_the_longest():
    key = "*|profile:77"
    for _ in range(12):
        accounts_api._ATTEMPTS[key] = [time.time()] * accounts_api._PROFILE_MAX_ATTEMPTS
        accounts_api.strike_if_spent(key, accounts_api._PROFILE_MAX_ATTEMPTS)
    _strikes, until = accounts_api._LOCKOUTS[key]
    assert until - time.time() <= accounts_api._LONGEST_PAUSE + 1


# --- L7. A successor proves themselves with something they already hold --------

SAM = ("sam", "sampassword9")


@pytest.fixture()
def household(app, people):
    """Dad (admin), Maya (family, the successor) — as in test_handover.py."""
    return people


def save_plan(client, household, wait_days=0):
    plan = {"message": "Look after the photographs.", "backups_where": "The grey disk.",
            "secrets_where": "The blue folder.", "password_where": "Ask Amma.",
            "machine_access": "Under the stairs.", "hardware": "A Mac mini",
            "successors": [household["family"].id], "helpers": []}
    response = client.post("/api/admin/handover", json={"plan": plan, "wait_days": wait_days})
    assert response.status_code == 200, response.get_json()


def make_code(client) -> str:
    response = client.post("/api/admin/handover/code")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["code"]


def session_for(app, conn, user_id: int):
    client = app.test_client()
    token, _ = auth.start_session(conn, user_id, "test", face="home")
    client.set_cookie(auth.SESSION_COOKIE, token)
    return client


def test_a_tap_to_enter_successor_cannot_claim_with_a_password_made_up_on_the_spot(app, household):
    """Whoever found the profile open on the family tablet, with the printed
    sheet, could type any password and start the takeover."""
    conn = household["conn"]
    conn.execute("UPDATE users SET password=NULL, pin=NULL WHERE id=?", (household["family"].id,))
    conn.commit()
    admin = login(app.test_client(), *ADMIN)
    save_plan(admin, household)
    code = make_code(admin)
    maya = session_for(app, conn, household["family"].id)

    me = maya.get("/api/handover/me").get_json()
    assert me["successor"] and not me["has_password"] and not me["has_pin"]
    refused = maya.post("/api/handover/claim", json={"code": code, "password": "a-new-password-9"})
    assert refused.status_code == 403
    assert "PIN or a password" in refused.get_json()["error"]
    assert not handover.waiting(conn)
    assert auth.get_user(conn, household["family"].id).role == auth.ROLE_FAMILY
    assert not auth.get_user(conn, household["family"].id).has_password, "nothing was set"
    # The code is not used up by the refusal, and the refusal is on record.
    assert admin.get("/api/admin/handover").get_json()["has_code"] is True
    assert "handover_refused" in [r["action"] for r in conn.execute("SELECT action FROM audit")]

    # With a password of their own, the same code works.
    auth.set_password(conn, household["family"].id, FAMILY[1])
    accepted = maya.post("/api/handover/claim", json={"code": code, "password": FAMILY[1]})
    assert accepted.status_code == 200, accepted.get_json()
    assert auth.get_user(conn, household["family"].id).role == auth.ROLE_ADMIN


def test_a_successor_with_a_pin_must_give_the_pin(app, household):
    conn = household["conn"]
    conn.execute("UPDATE users SET password=NULL WHERE id=?", (household["family"].id,))
    conn.commit()
    auth.set_pin(conn, household["family"].id, "4927")
    admin = login(app.test_client(), *ADMIN)
    save_plan(admin, household)
    code = make_code(admin)
    maya = session_for(app, conn, household["family"].id)
    assert maya.get("/api/handover/me").get_json()["has_pin"] is True

    # A password alone — the old way in — is not the PIN.
    without = maya.post("/api/handover/claim", json={"code": code, "password": "a-new-password-9"})
    assert without.status_code == 403 and "PIN" in without.get_json()["error"]
    wrong = maya.post("/api/handover/claim",
                      json={"code": code, "pin": "0000", "password": "a-new-password-9"})
    assert wrong.status_code == 403 and "PIN" in wrong.get_json()["error"]
    assert not handover.waiting(conn)
    assert not auth.get_user(conn, household["family"].id).has_password

    right = maya.post("/api/handover/claim",
                      json={"code": code, "pin": "4927", "password": "a-new-password-9"})
    assert right.status_code == 200, right.get_json()
    assert auth.get_user(conn, household["family"].id).role == auth.ROLE_ADMIN
    assert auth.authenticate(conn, FAMILY[0], "a-new-password-9") is not None


def test_pin_guesses_at_the_claim_spend_the_pickers_allowance(app, household):
    """The same counter as the tile on the picker, so the claim is not a second
    place to walk a four-digit PIN."""
    conn = household["conn"]
    conn.execute("UPDATE users SET password=NULL WHERE id=?", (household["family"].id,))
    conn.commit()
    auth.set_pin(conn, household["family"].id, "4927")
    key = f"*|profile:{household['family'].id}"
    accounts_api._ATTEMPTS[key] = [time.time()] * accounts_api._PROFILE_MAX_ATTEMPTS
    with app.test_request_context("/"):
        assert accounts_api.verify_pin_limited(conn, household["family"].id, "4927") is None
    accounts_api._ATTEMPTS.pop(key)
    with app.test_request_context("/"):
        assert accounts_api.verify_pin_limited(conn, household["family"].id, "0000") is False
        assert accounts_api.verify_pin_limited(conn, household["family"].id, "4927") is True
        assert accounts_api.verify_pin_limited(conn, household["family"].id, "") is False


# --- L8. Two setup requests at once make one administrator ---------------------

def _slow_count(monkeypatch, delay: float = 0.2):
    """Widen the gap between the count and the insert, so the race is lost
    every time without the lock and never with it."""
    real = auth.count_active_admins

    def slow(conn):
        n = real(conn)
        time.sleep(delay)
        return n
    monkeypatch.setattr(auth, "count_active_admins", slow)
    return real


def test_two_bootstraps_at_once_make_exactly_one_administrator(scanned, monkeypatch):
    cfg, conn, _ = scanned
    real = _slow_count(monkeypatch)
    outcomes: list[str] = []
    gate = threading.Barrier(2)

    def attempt(name):
        gate.wait()
        try:
            auth.bootstrap_admin(db.connect(cfg.db_path), name, "correcthorse1", name)
            outcomes.append("made")
        except PermissionError as exc:
            outcomes.append(str(exc))
        finally:
            db.close_all()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("first", "second")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == [auth.ALREADY_SET_UP, "made"]
    assert real(conn) == 1


def test_two_setup_posts_at_once_one_signs_in_and_one_is_told_it_is_done(scanned, monkeypatch):
    from ninaivu import build_services, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    real = _slow_count(monkeypatch)
    services = build_services(cfg)
    services.scanner.stop()
    application = create_home_app(services)
    answers: list[tuple[int, str]] = []
    gate = threading.Barrier(2)

    def post(name):
        gate.wait()
        try:
            response = application.test_client().post(
                "/api/auth/setup", json={"username": name, "password": "correcthorse1", "name": name})
            answers.append((response.status_code, response.get_json().get("error", "")))
        finally:
            db.close_all()

    threads = [threading.Thread(target=post, args=(n,)) for n in ("first", "second")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(a[0] for a in answers) == [200, 409]
    assert next(a[1] for a in answers if a[0] == 409) == auth.ALREADY_SET_UP
    assert real(conn) == 1
