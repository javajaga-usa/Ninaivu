"""First-run setup through a proxy on the same computer still asks for the code.

Caddy or Tailscale Serve on the Ninaivu computer connects from 127.0.0.1 and
says who the visitor really is in X-Forwarded-For. Waitress used to delete
that header before Ninaivu saw it (it drops forwarding headers from any peer
it was not told to trust), so the visitor through the proxy looked exactly
like a browser on this computer and could make themselves the administrator
without the code. These tests go through a real waitress server, the way
``python -m ninaivu`` runs, because the test client never had the problem.
"""

import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from ninaivu.server import auth

waitress = pytest.importorskip("waitress")


@pytest.fixture(autouse=True)
def _fresh_code(monkeypatch):
    monkeypatch.setattr(auth, "_SETUP_CODE", None)


@pytest.fixture
def served(scanned):
    from ninaivu import build_services, create_home_app
    from ninaivu.__main__ import waitress_server

    cfg, conn, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    server = waitress_server(app, "127.0.0.1", 0,
                             SimpleNamespace(server_threads=2, max_upload_mb=1))
    port = server.effective_port
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield port, cfg, conn
    finally:
        server.close()
        thread.join(timeout=5)


def _call(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        sent = {"Host": "localhost", **(headers or {})}
        if payload is not None:
            sent["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=sent)
        answer = connection.getresponse()
        return answer.status, json.loads(answer.read() or b"null")
    finally:
        connection.close()


#: What Caddy's reverse_proxy and Tailscale Serve add on the way in.
PROXIED = {"X-Forwarded-For": "100.101.102.103", "X-Forwarded-Host": "localhost",
           "X-Forwarded-Proto": "http"}


def test_a_browser_on_this_computer_is_not_asked_for_the_code(served):
    port, _, _ = served
    status, state = _call(port, "GET", "/api/auth/state")
    assert status == 200
    assert state["setup_required"] is True
    assert state["setup_code_required"] is False


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded"])
def test_through_a_proxy_on_this_computer_the_code_is_asked(served, header):
    port, cfg, conn = served
    headers = dict(PROXIED) if header == "X-Forwarded-For" else {
        "Forwarded": "for=100.101.102.103;host=localhost;proto=http"}
    status, state = _call(port, "GET", "/api/auth/state", headers)
    assert status == 200
    assert state["setup_code_required"] is True

    body = {"username": "stranger", "password": "correcthorse1", "name": "Stranger"}
    status, answer = _call(port, "POST", "/api/auth/setup", headers, body)
    assert status == 403
    assert answer["setup_code_required"] is True
    assert auth.needs_setup(conn), "no administrator was made"

    body["setup_code"] = auth.setup_code(cfg.state_dir)
    status, _ = _call(port, "POST", "/api/auth/setup", headers, body)
    assert status == 200
    assert not auth.needs_setup(conn)
