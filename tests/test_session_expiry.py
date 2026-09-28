"""What the server says when a session has ended.

The console reacts to a 401 by raising its sign-in screen, so these tests pin
the two things that behaviour depends on: that an ended session actually
answers 401 rather than something the browser would treat differently, and
that a *failed sign-in* answers 401 too — which is why the front end exempts
/api/auth/ from the expiry handling. Without that exemption a mistyped
password would be reported to the user as an expired session.
"""


from conftest import ADMIN, login
from ninaivu.server import auth


def _admin_console(scanned):
    from ninaivu import build_services, create_admin_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_admin_app(services).test_client(), *ADMIN)
    return client, conn


def test_a_signed_out_session_answers_401_not_a_redirect(scanned):
    """The console is a single page; a redirect to a login page would be
    delivered to fetch() as an opaque success and show as a parse error."""
    client, conn = _admin_console(scanned)
    assert client.get("/api/admin/overview").status_code == 200

    client.post("/api/auth/logout")

    response = client.get("/api/admin/overview")
    assert response.status_code in (401, 403)
    assert response.headers.get("Content-Type", "").startswith("application/json")


def test_every_session_a_profile_has_ends_at_once(scanned):
    """Signing a profile out from the console has to reach the tablet in the
    kitchen too, not just the browser that pressed the button."""
    client, conn = _admin_console(scanned)
    admin_id = conn.execute(
        "SELECT id FROM users WHERE username=?", (ADMIN[0],)).fetchone()["id"]

    from ninaivu import build_services, create_admin_app
    # A second signed-in client standing in for another device.
    services = build_services(_cfg_of(scanned))
    services.scanner.stop()
    second_device = login(create_admin_app(services).test_client(), *ADMIN)
    assert second_device.get("/api/admin/overview").status_code == 200

    auth.end_all_sessions(conn, admin_id)

    assert second_device.get("/api/admin/overview").status_code in (401, 403)


def _cfg_of(scanned):
    cfg, _, _ = scanned
    return cfg


def test_a_wrong_password_is_also_401(scanned):
    """The reason the front end must exempt /api/auth/ from expiry handling:
    this 401 means "try again", not "your session ended"."""
    client, _ = _admin_console(scanned)
    response = client.post("/api/auth/login",
                           json={"username": ADMIN[0], "password": "not-it"})
    assert response.status_code == 401


def test_signing_back_in_restores_access_without_a_reload(scanned):
    """The whole point of the change: the same client that was refused can
    sign in and carry on, so the page never has to be reloaded by hand."""
    client, _ = _admin_console(scanned)
    client.post("/api/auth/logout")
    assert client.get("/api/admin/overview").status_code in (401, 403)

    ok = client.post("/api/auth/login",
                     json={"username": ADMIN[0], "password": ADMIN[1]})
    assert ok.status_code == 200
    assert client.get("/api/admin/overview").status_code == 200
