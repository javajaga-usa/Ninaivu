"""The Cloud tab's endpoints: who can reach them, and what they hand out.

The sync engine itself is tested in ``test_cloud_sync.py``. This is about the
web layer, where a different set of things can go wrong:

* a route that exists on the family port when it should not exist at all
* a non-administrator reaching one on the console
* a refresh token escaping in a status response
* an OAuth callback accepting a code that did not come from this console

The last one is the subtle one. Without a state check, any page open in the
administrator's browser could send them to ``/api/cloud/callback?code=…`` with
a code for an account of the attacker's choosing, and the household's
photographs would start uploading to a stranger's Drive.
"""

from __future__ import annotations

import sys

import pytest

from ninaivu.server import auth
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.cloud import store

from conftest import ADMIN, FAMILY, GUEST, login


CLOUD_ROUTES = [
    ("get", "/api/cloud/status"),
    ("get", "/api/cloud/callback"),
    ("post", "/api/cloud/settings"),
    ("post", "/api/cloud/client"),
    ("post", "/api/cloud/connect"),
    ("post", "/api/cloud/disconnect"),
    ("post", "/api/cloud/start"),
    ("post", "/api/cloud/pause"),
    ("post", "/api/cloud/retry"),
    ("post", "/api/cloud/queue"),
]


@pytest.fixture()
def console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    return login(app.test_client(), *ADMIN), cfg, services


# --- who can reach it ------------------------------------------------------

@pytest.mark.parametrize("method,path", CLOUD_ROUTES)
def test_the_family_app_does_not_route_the_cloud_at_all(scanned, method, path):
    """404 on port 5000: absent, not forbidden.

    These routes hand out a Google consent URL and can start copying the
    household's photographs off the premises. The gallery should not know they
    exist.
    """
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    home = login(create_home_app(services).test_client(), *ADMIN)
    assert getattr(home, method)(path, json={}).status_code == 404


@pytest.mark.parametrize("who", ["family", "guest"])
@pytest.mark.parametrize("method,path", CLOUD_ROUTES)
def test_only_an_admin_may_use_the_cloud(app, people, who, method, path):
    client = login(app.test_client(), *(FAMILY if who == "family" else GUEST))
    assert getattr(client, method)(path, json={}).status_code in (401, 403)


# --- what status hands out -------------------------------------------------

def test_status_never_contains_a_token(console):
    client, cfg, services = console
    services.cloud.creds.client_id = "cid.apps.googleusercontent.com"
    services.cloud.creds.client_secret = "SECRET-SECRET"
    services.cloud.creds.refresh_token = "REFRESH-TOKEN"
    services.cloud.creds.access_token = "ACCESS-TOKEN"

    body = client.get("/api/cloud/status").get_data(as_text=True)
    for secret in ("SECRET-SECRET", "REFRESH-TOKEN", "ACCESS-TOKEN"):
        assert secret not in body, f"{secret} leaked into the console"
    # The front of the client ID identifies the Google project, so that is
    # what the console shows back — never the secret half of anything.
    assert "cid.apps.goog" in body


def test_status_says_where_google_must_send_the_browser_back(console):
    """The administrator has to paste this into their Google project, so it
    has to be the exact string — composed from the address they are on."""
    client, _, _ = console
    data = client.get("/api/cloud/status").get_json()
    assert data["redirect_uri"].endswith("/api/cloud/callback")


def test_it_starts_switched_off(console):
    client, _, _ = console
    data = client.get("/api/cloud/status").get_json()
    assert data["enabled"] is False
    assert data["account"]["connected"] is False


def test_the_address_the_console_was_opened_on_is_the_callback(console):
    client, _, _ = console
    data = client.get("/api/cloud/status").get_json()
    assert data["redirect_uri"] == "http://localhost/api/cloud/callback"
    assert data["redirect_uri_ok"] is True
    assert data["redirect_uri_problem"] is None


@pytest.mark.parametrize("host,scheme", [
    ("ninaivu-admin.local:3000", "https"),       # nothing outside the house resolves it
    ("192.168.0.119:3000", "https"),            # a private address
    ("192.168.0.119:3000", "http"),
])
def test_an_address_google_refuses_is_explained_not_attempted(scanned, host, scheme):
    """Google answers every one of these "redirect_uri_mismatch" and no more."""
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    base = f"{scheme}://{host}"
    # The session cookie belongs to the host it was issued on, so signing in
    # happens on the same address the console is being read from.
    client = app.test_client()
    signed_in = client.post("/api/auth/login", base_url=base,
                            json={"username": ADMIN[0], "password": ADMIN[1]})
    assert signed_in.status_code == 200, signed_in.get_json()
    status = client.get("/api/cloud/status", base_url=base).get_json()
    assert status["redirect_uri_ok"] is False
    assert status["redirect_uri_problem"]
    assert status["redirect_uri_loopback"].startswith("http")

    response = client.post("/api/cloud/connect", base_url=base)
    assert response.status_code == 409
    body = response.get_json()
    assert "localhost" in body["error"]
    assert body["redirect_uri"] == f"{base}/api/cloud/callback"


def test_the_loopback_address_carries_the_console_port(console):
    client, cfg, _ = console
    cfg.admin_port = 3000
    data = client.get("/api/cloud/status").get_json()
    assert data["redirect_uri_loopback"] == "http://localhost:3000/api/cloud/callback"


# --- settings --------------------------------------------------------------

def test_switching_it_on_is_remembered(console):
    client, cfg, _ = console
    import json
    client.post("/api/cloud/settings", json={"enabled": True})
    assert cfg.cloud_enabled is True
    # And it survives a restart, which is the part that actually matters.
    stored = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    assert stored["cloud_enabled"] is True


def test_the_folder_can_be_renamed_and_the_old_id_is_dropped(console):
    client, _, services = console
    services.cloud.creds.folder_id = "old-folder-id"
    data = client.post("/api/cloud/settings",
                       json={"folder_name": "Family photos"}).get_json()
    assert data["folder_name"] == "Family photos"
    assert services.cloud.creds.folder_id == ""


def test_the_client_id_and_secret_are_both_required(console):
    client, _, _ = console
    assert client.post("/api/cloud/client",
                       json={"client_id": "x"}).status_code == 400


def test_saving_the_client_makes_connect_available(console):
    client, _, _ = console
    assert client.post("/api/cloud/connect").status_code == 400
    client.post("/api/cloud/client",
                json={"client_id": "cid", "client_secret": "sec"})
    data = client.post("/api/cloud/connect").get_json()
    assert data["url"].startswith("https://accounts.google.com/")
    assert "drive.file" in data["url"]
    assert "access_type=offline" in data["url"]


# --- the callback ----------------------------------------------------------

def test_a_callback_without_the_state_we_issued_is_refused(console):
    """The check that stops a page in the browser from connecting an account
    nobody in this house asked for."""
    client, _, services = console
    client.post("/api/cloud/client",
                json={"client_id": "cid", "client_secret": "sec"})
    client.post("/api/cloud/connect")

    response = client.get("/api/cloud/callback?code=stolen&state=not-ours")
    assert response.status_code == 302
    assert "error=" in response.headers["Location"]
    assert services.cloud.creds.connected is False


def test_a_callback_google_refused_lands_back_on_the_tab(console):
    client, _, _ = console
    response = client.get("/api/cloud/callback?error=access_denied")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/#cloud")


# --- running it ------------------------------------------------------------

def test_starting_while_it_is_switched_off_is_refused(console):
    client, _, _ = console
    assert client.post("/api/cloud/start").status_code == 409


def test_starting_without_an_account_says_so_rather_than_failing_quietly(console):
    client, _, _ = console
    client.post("/api/cloud/settings", json={"enabled": True})
    response = client.post("/api/cloud/start")
    assert response.status_code == 409
    assert "connected" in response.get_json()["error"]


def test_the_queue_is_built_from_the_library(console):
    client, cfg, services = console
    data = client.post("/api/cloud/queue").get_json()
    assert data["queued"] > 0
    assert data["queue"]["pending"] == data["queued"]


def test_hidden_items_are_not_even_queued(console):
    """Two gates, not one: they are left out of the queue, and the engine
    checks again at the moment of sending in case they changed since."""
    client, cfg, services = console
    conn = services.cloud._connect_db()
    conn.execute("UPDATE assets SET visibility=2 WHERE folder LIKE 'private%'")
    conn.commit()
    hidden = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE visibility=2").fetchone()["n"]
    assert hidden > 0

    client.post("/api/cloud/queue")
    queued = {r["rel_path"] for r in store.recent(conn, limit=1000)}
    assert not any(p.startswith("private") for p in queued)


def test_queueing_twice_does_not_double_the_queue(console):
    client, _, _ = console
    first = client.post("/api/cloud/queue").get_json()["queued"]
    second = client.post("/api/cloud/queue").get_json()["queued"]
    assert first > 0 and second == 0


def test_disconnecting_keeps_the_record_of_what_went_up(console):
    """Otherwise reconnecting would send the whole library a second time."""
    client, _, services = console
    conn = services.cloud._connect_db()
    store.remember(conn, "/lib", "a.jpg", size=1)
    store.record_done(conn, "/lib", "a.jpg", remote_id="x")

    services.cloud.creds.refresh_token = "r"
    client.post("/api/cloud/disconnect", json={})

    assert store.state_of(conn, "/lib", "a.jpg") == store.DONE
    assert services.cloud.creds.connected is False


def test_the_credentials_file_is_owner_only(console):
    import stat
    if sys.platform == "win32":
        # os.chmod has no mode bits to set on Windows; the file's protection
        # there is the user profile's ACL on the state directory.
        pytest.skip("POSIX permissions do not apply")
    client, cfg, services = console
    client.post("/api/cloud/client",
                json={"client_id": "cid", "client_secret": "sec"})
    path = services.cloud.creds_path
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)
