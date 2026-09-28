"""Moving between the gallery and the console in one browser.

The two apps are separate security faces but they are not separate browsers.
Cookies are scoped to a host and never to a port, so both faces share one
cookie jar. They used to share the cookie *name* as well, which meant the
second sign-in silently overwrote the first: an admin who used the gallery
after signing in to the console found the console asking for a password again,
every single time.

Giving each face its own cookie name fixes that without touching the boundary.
These tests hold both halves in place — that the two sessions now coexist, and
that they still cannot be substituted for one another.
"""

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu.server import auth


@pytest.fixture()
def both(scanned):
    """Both faces over one set of services, as in production."""
    from ninaivu import build_services, create_admin_app, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    return create_home_app(services), create_admin_app(services), conn


class Browser:
    """One cookie jar shared by both faces, which is what a browser is."""

    def __init__(self, home_app, admin_app):
        self.home = home_app.test_client()
        self.admin = admin_app.test_client()
        self.jar = {}

    def _send(self, client, method, path, **kw):
        for name, value in self.jar.items():
            client.set_cookie(name, value, domain="localhost")
        response = getattr(client, method)(path, **kw)
        for header in response.headers.getlist("Set-Cookie"):
            name, _, rest = header.partition("=")
            value = rest.split(";")[0]
            if "Max-Age=0" in header or "Expires=Thu, 01 Jan 1970" in header:
                self.jar.pop(name, None)
            else:
                self.jar[name] = value
        return response

    def on_home(self, method, path, **kw):
        return self._send(self.home, method, path, **kw)

    def on_admin(self, method, path, **kw):
        return self._send(self.admin, method, path, **kw)


def test_the_two_faces_use_different_cookie_names():
    assert auth.cookie_name("home") != auth.cookie_name("admin")


def test_using_the_gallery_no_longer_signs_you_out_of_the_console(both):
    """The bug, stated as a test.

    Sign in to the console, then use the gallery on the same machine. The
    console must still be signed in — this is what made clicking through to
    the console ask for a password every time.
    """
    home_app, admin_app, _ = both
    browser = Browser(home_app, admin_app)

    browser.on_admin("post", "/api/auth/login",
                     json={"username": ADMIN[0], "password": ADMIN[1]})
    assert browser.on_admin("get", "/api/admin/overview").status_code == 200

    # …now the same person opens the gallery and signs in there too.
    browser.on_home("post", "/api/auth/login",
                    json={"username": ADMIN[0], "password": ADMIN[1]})
    assert browser.on_home("get", "/api/me").status_code == 200

    # …and the console is still theirs, with no second sign-in.
    assert browser.on_admin("get", "/api/admin/overview").status_code == 200


def test_both_cookies_are_present_at_once(both):
    home_app, admin_app, _ = both
    browser = Browser(home_app, admin_app)
    browser.on_admin("post", "/api/auth/login",
                     json={"username": ADMIN[0], "password": ADMIN[1]})
    browser.on_home("post", "/api/auth/login",
                    json={"username": ADMIN[0], "password": ADMIN[1]})
    assert auth.SESSION_COOKIE in browser.jar
    assert auth.ADMIN_SESSION_COOKIE in browser.jar


def test_signing_out_of_one_leaves_the_other_alone(both):
    home_app, admin_app, _ = both
    browser = Browser(home_app, admin_app)
    browser.on_admin("post", "/api/auth/login",
                     json={"username": ADMIN[0], "password": ADMIN[1]})
    browser.on_home("post", "/api/auth/login",
                    json={"username": ADMIN[0], "password": ADMIN[1]})

    browser.on_home("post", "/api/auth/logout")

    assert browser.on_admin("get", "/api/admin/overview").status_code == 200


# --- the boundary is unchanged --------------------------------------------

def test_a_family_app_session_still_cannot_open_the_console(both):
    """The property the whole two-port design rests on. Separate cookie names
    make both sessions survive; they must not make either one transferable."""
    home_app, admin_app, _ = both
    home = home_app.test_client()
    login(home, *ADMIN)
    cookie = home.get_cookie(auth.SESSION_COOKIE, domain="localhost")

    console = admin_app.test_client()
    console.set_cookie(auth.ADMIN_SESSION_COOKIE, cookie.value, domain="localhost")
    assert console.get("/api/admin/overview").status_code == 401


def test_a_family_members_session_cannot_open_the_console(both):
    home_app, admin_app, conn = both
    auth.create_user(conn, FAMILY[0], FAMILY[1],
                     display_name="Maya", role="family")
    home = home_app.test_client()
    login(home, *FAMILY)
    cookie = home.get_cookie(auth.SESSION_COOKIE, domain="localhost")

    console = admin_app.test_client()
    console.set_cookie(auth.ADMIN_SESSION_COOKIE, cookie.value, domain="localhost")
    assert console.get("/api/admin/overview").status_code in (401, 403)


def test_an_older_console_session_under_the_shared_name_still_works(both):
    """Upgrading must not sign every admin out of a console they were already
    using — an existing session under the old name is still honoured, and the
    face check still applies to it."""
    home_app, admin_app, conn = both
    console = admin_app.test_client()
    login(console, *ADMIN)
    token = console.get_cookie(auth.ADMIN_SESSION_COOKIE, domain="localhost").value

    legacy = admin_app.test_client()
    legacy.set_cookie(auth.SESSION_COOKIE, token, domain="localhost")
    assert legacy.get("/api/admin/overview").status_code == 200
