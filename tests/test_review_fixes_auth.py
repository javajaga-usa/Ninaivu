"""Sign-in, share-link and upload-name fixes from the security review.

Each test states one of the defects as it was found: the first-run setup
trusting a proxy's loopback address, share-link and step-up password guesses
limited racily or not at all, the account-wide sign-in limit that anybody on
the network could spend for the administrator, a reused profile id inheriting
a deleted person's albums and backups, the console's sign-out ending the
gallery's session, and Tamil file names reduced to nothing.
"""

import time

import pytest

from conftest import ADMIN, FAMILY, ids_of, login
from ninaivu.server import auth
from ninaivu.utils.filenames import safe_filename


def _home_app(scanned):
    from ninaivu import build_services, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    return create_home_app(services), cfg, conn


# --- 1. The first administrator, behind a proxy ------------------------------

@pytest.mark.parametrize("header", ["X-Forwarded-For", "CF-Connecting-IP",
                                    "Tailscale-Funnel-Request", "Forwarded"])
def test_a_proxied_request_from_loopback_still_needs_the_setup_code(scanned, header):
    """Funnel, Caddy and a Cloudflare tunnel all arrive from 127.0.0.1."""
    app, _, conn = _home_app(scanned)
    client = app.test_client()
    headers = {header: "203.0.113.9"}
    state = client.get("/api/auth/state", headers=headers).get_json()
    assert state["setup_code_required"] is True
    body = {"username": "mallory", "password": "correcthorse1", "name": "M"}
    assert client.post("/api/auth/setup", json=body, headers=headers).status_code == 403
    assert auth.needs_setup(conn)


def test_a_trusted_proxy_is_believed(scanned):
    """With trusted_proxies set, ProxyFix has put the real address in place."""
    app, cfg, _ = _home_app(scanned)
    cfg.trusted_proxies = 1
    client = app.test_client()
    # The proxy on this computer says the visitor is this computer too.
    state = client.get("/api/auth/state",
                       headers={"X-Forwarded-For": "127.0.0.1"}).get_json()
    assert state["setup_code_required"] is False


def test_a_setup_code_with_non_ascii_characters_is_refused_not_a_crash(scanned):
    app, _, conn = _home_app(scanned)
    far = {"REMOTE_ADDR": "192.168.1.60"}
    body = {"username": "mallory", "password": "correcthorse1", "name": "M",
            "setup_code": "கதவு-é"}
    response = app.test_client().post("/api/auth/setup", json=body, environ_base=far)
    assert response.status_code == 403
    assert auth.needs_setup(conn)


# --- 2. Share-link passwords -------------------------------------------------

def _share(client, **extra):
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    response = client.post("/api/shares",
                           json={"scope": "asset", "target_id": asset_id, **extra})
    return response


def test_a_new_share_password_must_not_be_trivially_short(as_family):
    assert _share(as_family, password="abc").status_code == 400
    assert _share(as_family, password="abcd").status_code == 200
    assert _share(as_family).status_code == 200         # no password at all


def test_rotating_addresses_does_not_reset_a_share_links_limit(app, as_family):
    share = _share(as_family, password="open-sesame").get_json()
    anon = app.test_client()
    codes = []
    for attempt in range(40):
        address = f"2001:db8::{attempt // 4 + 1:x}"
        codes.append(anon.post(f"/api/share/{share['token']}/unlock",
                               json={"password": f"wrong-{attempt}"},
                               environ_base={"REMOTE_ADDR": address}).status_code)
    assert codes.count(429) >= 15, "the link's own ceiling was never reached"


def test_the_header_password_counts_against_the_same_limit(app, as_family):
    from ninaivu.api import accounts_api

    share = _share(as_family, password="open-sesame").get_json()
    anon = app.test_client()
    for _ in range(3):
        anon.get(f"/api/share/{share['token']}",
                 headers={"X-Share-Password": "wrong"})
    assert len(accounts_api._ATTEMPTS[f"*|share:{share['token']}"]) == 3
    ok = anon.get(f"/api/share/{share['token']}",
                  headers={"X-Share-Password": "open-sesame"})
    assert ok.status_code == 200


# --- 3/4. Step-up passwords share one allowance -----------------------------

def test_guessing_the_password_on_delete_is_limited(as_admin):
    target = ids_of(as_admin)[:1]
    codes = [as_admin.post("/api/delete", json={"ids": target,
                                                "password": f"wrong-{i}"}).status_code
             for i in range(12)]
    assert codes[0] == 401
    assert 429 in codes[8:]
    # …and the same allowance covers changing the password.
    response = as_admin.post("/api/me/password",
                             json={"current": ADMIN[1], "password": "anotherhorse2"})
    assert response.status_code == 429


def test_guessing_the_password_on_emptying_the_bin_is_limited(as_admin):
    codes = [as_admin.post("/api/recycle/purge", json={"ids": [1],
                                                       "password": f"wrong-{i}"}).status_code
             for i in range(12)]
    assert 429 in codes


def test_a_right_password_gives_the_allowance_back(as_admin):
    from ninaivu.api import accounts_api

    target = ids_of(as_admin)[:1]
    as_admin.post("/api/delete", json={"ids": target, "password": "wrong"})
    key = next(k for k in accounts_api._ATTEMPTS if k.startswith("password|"))
    assert len(accounts_api._ATTEMPTS[key]) == 1
    response = as_admin.post("/api/me/password",
                             json={"current": ADMIN[1], "password": "anotherhorse2"})
    assert response.status_code == 200
    assert key not in accounts_api._ATTEMPTS


def test_a_wrong_pin_at_the_lock_screen_counts_for_the_whole_profile(as_family, people):
    from ninaivu.api import accounts_api

    member = people["family"].id
    auth.set_pin(people["conn"], member, "4321")
    assert as_family.post("/api/auth/unlock", json={"secret": "0000"}).status_code == 403
    assert len(accounts_api._ATTEMPTS[f"*|profile:{member}"]) == 1


def test_a_paused_profile_cannot_be_unlocked_either(as_family, people):
    from ninaivu.api import accounts_api

    member = people["family"].id
    auth.set_pin(people["conn"], member, "4321")
    accounts_api._LOCKOUTS[f"*|profile:{member}"] = (1, time.time() + 600)
    assert as_family.post("/api/auth/unlock", json={"secret": "4321"}).status_code == 401


# --- 5. The account-wide sign-in limit --------------------------------------

def test_a_spent_account_allowance_still_lets_this_computer_sign_in(app, people):
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS[f"*|user:{ADMIN[0]}"] = [time.time()] * 20
    far = app.test_client()
    response = far.post("/api/auth/login",
                        json={"username": ADMIN[0], "password": ADMIN[1]},
                        environ_base={"REMOTE_ADDR": "192.168.1.90"})
    assert response.status_code == 429, "elsewhere, still refused unchecked"
    assert f"192.168.1.90|{ADMIN[0].lower()}" not in accounts_api._ATTEMPTS, \
        "a refused attempt spends nothing"
    login(app.test_client(), *ADMIN)            # from 127.0.0.1: let in


def test_a_spent_allowance_is_not_bypassed_through_a_proxy(app, people):
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS[f"*|user:{ADMIN[0]}"] = [time.time()] * 20
    response = app.test_client().post(
        "/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
        headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 429


# --- 6. Deleting a profile ---------------------------------------------------

def test_deleting_a_profile_leaves_nothing_for_the_next_one_to_inherit(
        app, people, as_admin):
    from ninaivu.media import phone_backup

    conn = people["conn"]
    member = people["family"].id
    admin = people["admin"].id
    conn.execute("INSERT INTO albums(name, created_at, created_by) VALUES(?,?,?)",
                 ("Maya's trip", time.time(), member))
    conn.execute("INSERT INTO shares(token, scope, target_id, created_at, created_by) "
                 "VALUES('tok-maya', 'asset', 1, ?, ?)", (time.time(), member))
    phone_backup.init_schema(conn)
    conn.execute("INSERT INTO phone_backups(user_id, device_id, fingerprint, filename, "
                 "size, state) VALUES(?, 'phone-1234', 'a.jpg|1|0', 'a.jpg', 1, 'done')",
                 (member,))
    conn.commit()

    assert as_admin.delete(f"/api/people/{member}").status_code == 200

    album = conn.execute("SELECT created_by FROM albums WHERE name=?",
                         ("Maya's trip",)).fetchone()
    assert album["created_by"] == admin, "the album passes to an administrator"
    assert conn.execute("SELECT created_by FROM shares WHERE token='tok-maya'"
                        ).fetchone()["created_by"] is None
    assert conn.execute("SELECT COUNT(*) n FROM phone_backups WHERE user_id=?",
                        (member,)).fetchone()["n"] == 0


def test_delete_user_on_its_own_hands_albums_to_an_administrator(app, people):
    conn = people["conn"]
    member = people["family"].id
    conn.execute("INSERT INTO albums(name, created_at, created_by) VALUES(?,?,?)",
                 ("Maya's garden", time.time(), member))
    conn.commit()
    auth.delete_user(conn, member)
    row = conn.execute("SELECT created_by FROM albums WHERE name=?",
                       ("Maya's garden",)).fetchone()
    assert row["created_by"] == people["admin"].id


# --- 7. Signing out of the console -------------------------------------------

def test_signing_out_of_the_console_leaves_the_gallery_session_alone(scanned):
    from ninaivu import build_services, create_admin_app, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY)
    services = build_services(cfg)
    services.scanner.stop()
    home = login(create_home_app(services).test_client(), *FAMILY)
    cookie = home.get_cookie(auth.SESSION_COOKIE, domain="localhost")

    console = create_admin_app(services).test_client()
    console.set_cookie(auth.SESSION_COOKIE, cookie.value, domain="localhost")
    response = console.post("/api/auth/logout")
    assert response.status_code == 200
    # The gallery's cookie is not deleted, and its session is not ended.
    assert not any(h.startswith(auth.SESSION_COOKIE + "=")
                   for h in response.headers.getlist("Set-Cookie"))
    assert auth.session_face(conn, cookie.value) == "home"
    assert home.get("/api/auth/state").get_json()["signed_in"] is True


# --- 9. File names in any script --------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("நாள்.jpg", "நாள்.jpg"),
    ("../../etc/நாள்.jpg", "நாள்.jpg"),
    ("C:\\photos\\x.jpg", "x.jpg"),
    ("a<b>c:d\"e|f?g*.png", "abcdefg.png"),
    (".hidden.jpg", "hidden.jpg"),
    ("trailing.jpg. . ", "trailing.jpg"),
    ("bad\x00\x1fname\u202e.jpg", "badname.jpg"),
    ("CON", ""), ("nul.jpg", ""), ("Com1.txt", ""), ("lpt9", ""),
    ("console.jpg", "console.jpg"),
    ("", ""), ("...", ""), ("/", ""),
])
def test_safe_filename(given, expected):
    assert safe_filename(given) == expected


def test_safe_filename_normalises_and_limits_length():
    decomposed = "cafe\u0301.jpg"
    assert safe_filename(decomposed) == "caf\u00e9.jpg"
    long = "அ" * 400 + ".jpeg"
    cut = safe_filename(long)
    assert len(cut) <= 200 and cut.endswith(".jpeg") and cut.startswith("அ")


def test_a_phone_backup_named_in_tamil_is_not_unsupported(as_family):
    response = as_family.post("/api/phone-backup/check", json={
        "device_id": "phone-abcd1234",
        "files": [{"name": "நாள்.jpg", "size": 10, "modified": 0},
                  {"name": "IMG 0001.jpg", "size": 10, "modified": 0}]})
    assert response.status_code == 200, response.get_json()
    states = [f["state"] for f in response.get_json()["files"]]
    assert states == ["new", "new"]


@pytest.mark.parametrize("given, expected", [
    ("café.jpg", "cafe.jpg"),              # the old ASCII name, so no resend
    ("IMG 0001.jpg", "IMG_0001.jpg"),
    ("நாள்.jpg", "நாள்.jpg"),              # the old name was unusable
])
def test_a_phone_backup_name_keeps_its_old_form_where_that_worked(given, expected):
    from ninaivu.api.api_phone_backup import _backup_name
    assert _backup_name(given) == expected
