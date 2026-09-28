"""The two-port split, and the profile-picker sign-in.

The important property here is *isolation*: the family app on 5000 must not
merely refuse admin endpoints, it must not have them. A 404 rather than a 403
means there is nothing there to attack.
"""

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.server import auth
from ninaivu import build_services, create_admin_app, create_home_app


@pytest.fixture()
def apps(scanned):
    """Both faces, sharing one set of services — as they do in production."""
    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    home = create_home_app(services)
    admin = create_admin_app(services)
    return {"home": home, "admin": admin, "cfg": cfg, "conn": conn}


def test_readiness_is_public_on_both_ports_even_when_library_is_private(apps):
    apps["cfg"].open_browsing = False

    for face in (apps["home"], apps["admin"]):
        response = face.test_client().get("/readyz")
        assert response.status_code == 200
        assert response.get_json()["ok"] is True


def test_home_renders_gallery_and_viewer_with_certificate_link(apps):
    response = apps["home"].test_client().get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    for marker in ('id="grid"', 'id="viewer"', '/static/js/app.js', 'href="/cert"'):
        assert marker in body



def test_both_certificate_links_are_plain_links_that_the_server_names(apps):
    """Opening /cert directly worked while the admin screen's button did not.

    The only difference was a download="" attribute, which hands the click to the
    browser's download checks -- and those can refuse a certificate from a site the
    browser does not trust yet, which is the one moment the link is needed. The
    server already sends the file as an attachment with the right name, so the
    links need nothing more than an href.
    """
    import re
    for face in ("home", "admin"):
        body = apps[face].test_client().get("/").get_data(as_text=True)
        links = re.findall(r'<a\s[^>]*href="/cert"[^>]*>', body)
        assert links, f"{face} page has no certificate link"
        for link in links:
            assert "download" not in link, (face, link)

    served = apps["admin"].test_client().get("/cert")
    if served.status_code == 200:
        assert served.headers["Content-Disposition"].startswith("attachment")
        assert "ninaivu-ca.crt" in served.headers["Content-Disposition"]


def test_the_admin_screen_says_where_windows_must_put_the_certificate(apps):
    """Windows' import wizard defaults to choosing a store itself, and files a
    home-made certificate authority under Intermediate, where it is never trusted."""
    body = apps["admin"].test_client().get("/").get_data(as_text=True)
    assert "Trusted Root Certification Authorities" in body

@pytest.fixture()
def staffed(apps):
    conn = apps["conn"]
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    family = auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                              role=auth.ROLE_FAMILY, created_by=admin.id)
    guest = auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                             role=auth.ROLE_GUEST, created_by=admin.id)
    kid = auth.create_user(conn, "sam", "", display_name="Sam",
                           role=auth.ROLE_FAMILY, created_by=admin.id, pin="4821")
    tap = auth.create_user(conn, "nan", "", display_name="Nan",
                           role=auth.ROLE_FAMILY, created_by=admin.id)
    return {**apps, "admin_user": admin, "family": family, "guest": guest,
            "kid": kid, "tap": tap}


# ---------------------------------------------------------------------------
# Port isolation
# ---------------------------------------------------------------------------

ADMIN_ONLY_PATHS = [
    "/api/admin/overview",
    "/api/admin/folders",
    "/api/admin/preview",
    "/api/people",
    "/api/people/folders",
    "/api/audit",
]


@pytest.mark.parametrize("path", ADMIN_ONLY_PATHS)
def test_admin_endpoints_do_not_exist_on_the_family_app(staffed, path):
    """Not forbidden — absent. The family app never routes these at all."""
    client = login(staffed["home"].test_client(), *ADMIN)
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ADMIN_ONLY_PATHS)
def test_admin_endpoints_exist_on_the_console(staffed, path):
    client = login(staffed["admin"].test_client(), *ADMIN)
    assert client.get(path).status_code == 200


def test_profile_picker_does_not_exist_on_the_console(staffed):
    client = login(staffed["admin"].test_client(), *ADMIN)
    assert client.get("/api/auth/profiles").status_code == 404
    assert client.post("/api/auth/enter", json={"id": 2}).status_code == 404


def test_console_refuses_non_admins_entirely(staffed):
    for credentials in (FAMILY, GUEST):
        client = staffed["admin"].test_client()
        response = client.post("/api/auth/login", json={
            "username": credentials[0], "password": credentials[1]})
        assert response.status_code == 403
        assert "administrator" in response.get_json()["error"].lower()


def test_console_blocks_anonymous_callers(staffed):
    client = staffed["admin"].test_client()
    assert client.get("/api/assets").status_code == 401
    assert client.get("/api/status").status_code == 401
    # The login page and its API stay reachable.
    assert client.get("/").status_code == 200
    assert client.get("/api/auth/state").status_code == 200


def test_console_blocks_a_signed_in_family_member(staffed):
    """A session made on the family app must not unlock the console."""
    home = login(staffed["home"].test_client(), *FAMILY)
    cookie = home.get_cookie(auth.SESSION_COOKIE)
    console = staffed["admin"].test_client()
    console.set_cookie(auth.SESSION_COOKIE, cookie.value)
    assert console.get("/api/admin/overview").status_code == 401


def test_a_home_session_never_opens_the_console_even_for_an_admin(staffed):
    """The README's promise: the split is a security boundary, not a layout.

    Cookies are not port-scoped, so the browser offers the family-app cookie
    to the console too; the session itself has to know which face made it.
    """
    home = login(staffed["home"].test_client(), *ADMIN)
    cookie = home.get_cookie(auth.SESSION_COOKIE)
    console = staffed["admin"].test_client()
    console.set_cookie(auth.SESSION_COOKIE, cookie.value)
    assert console.get("/api/admin/overview").status_code == 401
    # …while the same cookie keeps working on the face it was made on.
    assert home.get("/api/me").get_json()["username"] == ADMIN[0]


def test_a_console_session_is_not_a_family_session(staffed):
    """The boundary runs both ways: each session opens the app that made it.

    Each face now keeps its session under its own cookie name so the two can
    coexist in one browser, so the console's token is read from its own name —
    and then offered to the family app under *its* name, which is the closest
    thing to an attack this can be put to. It must still be refused, and it is
    refused by the face recorded on the session rather than by the cookie it
    arrived in.
    """
    console = login(staffed["admin"].test_client(), *ADMIN)
    cookie = console.get_cookie(auth.ADMIN_SESSION_COOKIE)
    assert cookie is not None, "the console should mint its own cookie"

    home = staffed["home"].test_client()
    home.set_cookie(auth.SESSION_COOKIE, cookie.value)
    assert home.get("/api/me").get_json()["anonymous"] is True

    # …and offering it under its own name to the family app fails too.
    home2 = staffed["home"].test_client()
    home2.set_cookie(auth.ADMIN_SESSION_COOKIE, cookie.value)
    assert home2.get("/api/me").get_json()["anonymous"] is True

    assert console.get("/api/me").get_json()["username"] == ADMIN[0]


def test_each_face_serves_its_own_page(staffed):
    home = staffed["home"].test_client().get("/").get_data(as_text=True)
    console = login(staffed["admin"].test_client(), *ADMIN).get("/").get_data(as_text=True)
    assert "static/js/app.js" in home
    assert "static/js/admin.js" in console
    assert "admin.css" in console
    assert "admin.css" not in home


def test_family_page_uses_favourites_without_star_ratings(staffed):
    home = staffed["home"].test_client().get("/").get_data(as_text=True)
    assert 'id="v-fav"' in home
    assert 'id="v-stars"' not in home
    assert 'value="rating_desc"' not in home
    assert "Rate / clear rating" not in home


def test_auth_state_reports_its_face(staffed):
    home = staffed["home"].test_client().get("/api/auth/state").get_json()
    console = staffed["admin"].test_client().get("/api/auth/state").get_json()
    assert home["face"] == "home"
    assert console["face"] == "admin"
    assert "profiles" in home
    assert "profiles" not in console, "the console must not advertise the household"


def test_both_faces_share_one_library(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    console.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "public", "confirm": True})

    home = staffed["home"].test_client()   # not signed in at all
    assert home.get("/api/assets").get_json()["total"] == 4


# ---------------------------------------------------------------------------
# Profile picker
# ---------------------------------------------------------------------------

def test_picker_lists_everyone_except_admins(staffed):
    data = staffed["home"].test_client().get("/api/auth/profiles").get_json()
    names = {p["name"] for p in data["profiles"]}
    assert names == {"Maya", "Neighbour", "Sam", "Nan"}
    assert "Dad" not in names, "admins sign in on the console, not from the picker"


def test_picker_shows_who_is_locked_but_not_how(staffed):
    profiles = staffed["home"].test_client().get("/api/auth/profiles").get_json()["profiles"]
    by_name = {p["name"]: p for p in profiles}

    assert by_name["Nan"]["locked"] is False and by_name["Nan"]["kind"] == "open"
    assert by_name["Sam"]["locked"] is True and by_name["Sam"]["kind"] == "pin"
    assert by_name["Maya"]["locked"] is True and by_name["Maya"]["kind"] == "password"
    for profile in profiles:
        assert "pin" not in profile or profile["kind"] == "pin"
        assert "password" not in profile


def test_tap_to_enter_profile_needs_no_secret(staffed):
    client = staffed["home"].test_client()
    response = client.post("/api/auth/enter", json={"id": staffed["tap"].id})
    assert response.status_code == 200
    assert response.get_json()["user"]["name"] == "Nan"
    assert client.get("/api/me").get_json()["role"] == "family"


def test_pin_profile_requires_the_pin(staffed):
    client = staffed["home"].test_client()
    kid_id = staffed["kid"].id
    assert client.post("/api/auth/enter",
                       json={"id": kid_id}).status_code == 401
    assert client.post("/api/auth/enter",
                       json={"id": kid_id, "secret": "0000"}).status_code == 401
    ok = client.post("/api/auth/enter", json={"id": kid_id, "secret": "4821"})
    assert ok.status_code == 200
    assert ok.get_json()["user"]["name"] == "Sam"


def test_password_profile_can_enter_from_the_picker(staffed):
    client = staffed["home"].test_client()
    response = client.post("/api/auth/enter", json={
        "id": staffed["family"].id, "secret": FAMILY[1]})
    assert response.status_code == 200


def test_picker_cannot_be_used_to_become_an_admin(staffed):
    client = staffed["home"].test_client()
    response = client.post("/api/auth/enter", json={
        "id": staffed["admin_user"].id, "secret": ADMIN[1]})
    assert response.status_code == 403
    assert "console" in response.get_json()["error"].lower()
    assert client.get("/api/me").get_json()["anonymous"] is True


def test_disabled_profile_is_off_the_picker(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    console.post(f"/api/people/{staffed['tap'].id}", json={"active": False})

    home = staffed["home"].test_client()
    names = {p["name"] for p in home.get("/api/auth/profiles").get_json()["profiles"]}
    assert "Nan" not in names
    assert home.post("/api/auth/enter",
                     json={"id": staffed["tap"].id}).status_code == 401


def test_picker_entry_is_rate_limited(staffed):
    client = staffed["home"].test_client()
    kid_id = staffed["kid"].id
    codes = [client.post("/api/auth/enter",
                         json={"id": kid_id, "secret": "9999"}).status_code
             for _ in range(10)]
    assert 429 in codes, "brute-forcing a 4-digit PIN must be throttled"


# ---------------------------------------------------------------------------
# PIN management
# ---------------------------------------------------------------------------

def test_admin_sets_and_clears_a_pin(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    person_id = staffed["tap"].id

    console.post(f"/api/people/{person_id}", json={"pin": "7391"})
    home = staffed["home"].test_client()
    assert home.post("/api/auth/enter", json={"id": person_id}).status_code == 401
    assert home.post("/api/auth/enter",
                     json={"id": person_id, "secret": "7391"}).status_code == 200

    console.post(f"/api/people/{person_id}", json={"pin": ""})
    assert staffed["home"].test_client().post(
        "/api/auth/enter", json={"id": person_id}).status_code == 200


def test_weak_pins_are_refused(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    for weak in ("1234", "0000", "12", "abcd"):
        response = console.post(f"/api/people/{staffed['tap'].id}", json={"pin": weak})
        assert response.status_code == 400


def test_only_admins_can_set_a_pin(staffed):
    home = login(staffed["home"].test_client(), *FAMILY)
    assert home.post(f"/api/people/{staffed['tap'].id}",
                     json={"pin": "5566"}).status_code == 404


# ---------------------------------------------------------------------------
# Console features
# ---------------------------------------------------------------------------

def test_overview_summarises_the_household(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    data = console.get("/api/admin/overview").get_json()
    assert data["people"]["total"] == 5
    assert data["people"]["by_role"]["admin"] == 1
    assert data["people"]["by_role"]["family"] == 3
    assert data["stats"]["count"] == 15
    assert data["app"]["home_url"].startswith("http://")


def test_preview_answers_what_will_they_see(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)

    before = console.get("/api/admin/preview?role=guest").get_json()
    assert before["total"] == 0

    console.post("/api/visibility/folder", json={
        "folder": "shared/holiday", "visibility": "public", "confirm": True})
    after = console.get("/api/admin/preview?role=guest").get_json()
    assert after["total"] == 4
    assert {item["folder"] for item in after["items"]} == {"shared/holiday"}


def test_preview_for_a_specific_person_uses_their_scope(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    console.post(f"/api/people/{staffed['family'].id}", json={"scope": "private"})

    data = console.get(f"/api/admin/preview?person={staffed['family'].id}").get_json()
    assert data["as"] == "Maya"
    assert data["scope"] == "private"
    assert data["total"] == 3


def test_folder_tree_reports_the_visibility_mix(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    console.post("/api/visibility/folder", json={
        "folder": "private", "visibility": "hidden", "confirm": True})

    folders = console.get("/api/admin/folders").get_json()["folders"]
    private = next(f for f in folders if f["path"] == "private")
    assert private["hidden"] == 3
    assert private["rule"] == "hidden"

    shared = next(f for f in folders if f["path"] == "shared")
    assert shared["family"] == 4
    assert shared["rule"] is None, "unset folders should not claim an explicit rule"


def test_console_settings_toggle_open_browsing(staffed):
    console = login(staffed["admin"].test_client(), *ADMIN)
    response = console.post("/api/admin/settings", json={"open_browsing": False})
    assert response.get_json()["settings"]["open_browsing"] is False
    assert staffed["cfg"].open_browsing is False

    # …and the family app immediately locks down.
    assert staffed["home"].test_client().get("/api/assets").status_code == 401


# ---------------------------------------------------------------------------
# Serving the house over the LAN
# ---------------------------------------------------------------------------

def test_lan_address_is_found_or_reported_as_absent():
    """The banner has to be able to tell people what to type on their phones."""
    from ninaivu.__main__ import lan_address

    found = lan_address()
    assert found is None or not found.startswith("127."), found


def test_the_overview_sends_the_family_port_not_just_a_guessed_url(as_admin):
    """Bound to 0.0.0.0 the server cannot know which address a browser used.

    The console builds the "Family app" link from its own hostname plus this
    port, so opening the console from a laptop does not produce a link to the
    laptop's own localhost.
    """
    app = as_admin.get("/api/admin/overview").get_json()["app"]
    assert isinstance(app["home_port"], int)
    assert app["home_port"] > 0
    assert "scheme" in app
    assert "hostnames" in app
    assert app["home_url"].startswith(f"{app['scheme']}://")


def test_auth_state_reports_scheme_and_hostnames(staffed):
    client = staffed["admin"].test_client()
    state = client.get("/api/auth/state").get_json()
    assert "scheme" in state
    assert "hostnames" in state
    assert "home_port" in state
    assert "admin_port" in state


def test_overview_home_url_respects_app_scheme_and_hostnames(staffed):
    admin_app = staffed["admin"]
    admin_app.config["MV_SCHEME"] = "https"
    admin_app.config["MV_HOSTNAMES"] = {"family": "myhome.local", "admin": "myhome-admin.local"}
    console = login(admin_app.test_client(), *ADMIN)
    app = console.get("/api/admin/overview").get_json()["app"]
    assert app["home_url"] == "https://myhome.local" or app["home_url"].startswith("https://myhome.local")
    assert app["scheme"] == "https"
    assert app["hostnames"]["family"] == "myhome.local"


def test_the_offline_screen_offers_a_check_the_service_worker_cannot_hide(apps):
    """A running Ninaivu whose certificate a browser does not trust looked
    exactly like a stopped one: the offline copy of the page hid the browser's
    own warning. The check opens /readyz, which the service worker leaves alone,
    so the browser shows what it really gets."""
    import re
    from pathlib import Path
    body = apps["home"].test_client().get("/").get_data(as_text=True)
    for element_id in ("offline-check", "empty-check"):
        link = re.search(r'<a\s[^>]*id="' + element_id + r'"[^>]*>', body)
        assert link, element_id
        assert 'href="/readyz"' in link.group(0) and 'target="_blank"' in link.group(0)
    assert apps["home"].test_client().get("/readyz").status_code == 200
    script = (Path(__file__).parents[1] / "ninaivu" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "ninaivu.local" not in script.split("function setServerOffline", 1)[1].split("async function checkConnection", 1)[0], \
        "the offline message names the address actually opened, not a fixed name"


def test_cloud_idle_checks_both_apps(apps, monkeypatch):
    from ninaivu.server import activity
    services = apps['home'].config['MV_SERVICES']
    monkeypatch.setattr(activity, 'other_work_active',
                        lambda services, config: config.get('TEST_BUSY', False))
    engine = services.cloud.engine()
    assert engine._is_idle()
    apps['home'].config['TEST_BUSY'] = True
    assert not engine._is_idle()
    apps['home'].config['TEST_BUSY'] = False
    apps['admin'].config['TEST_BUSY'] = True
    assert not engine._is_idle()
    apps['admin'].config['TEST_BUSY'] = False
    assert engine._is_idle()
