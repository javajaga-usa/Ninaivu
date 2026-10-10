"""Audit of 10 October 2026: the notification webhook cannot be used to read
the house's network through the console's "send a test" button."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ninaivu.utils import notify
from ninaivu.utils.notify import Notifier


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        # Read the body first: Windows resets a connection closed with the
        # request unread, and the client then reports nothing reachable.
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/secret")
            self.end_headers()
            return
        if self.path == "/secret":
            _Handler.reached_secret = True
        self.send_response(403)
        self.end_headers()
        self.wfile.write(b"router admin page: password=hunter2")

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    _Handler.reached_secret = False
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_an_error_answer_says_only_its_status(server):
    result = Notifier(webhook_url=f"{server}/page")._post("t", "d", "test")
    assert result == "The webhook answered HTTP 403."
    assert "hunter2" not in result


def test_a_redirect_is_not_followed(server):
    result = Notifier(webhook_url=f"{server}/redirect")._post("t", "d", "test")
    assert result == "The webhook answered HTTP 302."
    assert _Handler.reached_secret is False


def test_nothing_reachable_says_only_that():
    result = Notifier(webhook_url="http://127.0.0.1:9/")._post("t", "d", "test")
    assert result == "Could not reach the webhook."


def test_a_link_local_address_is_refused(monkeypatch):
    monkeypatch.setattr(notify.urllib.request.OpenerDirector, "open",
                        lambda *a, **k: pytest.fail("posted to a link-local address"))
    for url in ("http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/"):
        assert "link-local" in Notifier(webhook_url=url)._post("t", "d", "test")


def test_the_house_ntfy_on_the_lan_is_still_allowed():
    assert notify._resolve("192.168.1.10", 80)
    assert notify._resolve("127.0.0.1", 80)
