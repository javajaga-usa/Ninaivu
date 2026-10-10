"""Audit of 10 October 2026, the egress findings: where a request Ninaivu makes
on somebody's say-so may go, and what it keeps private on the way.

The three features that connect to an address a person typed — the AI server,
the notification webhook and the off-site bucket — now share one idea of an
address that is never a target (``ninaivu.utils.netscope``); the webhook
connects to the address it checked rather than looking the name up twice; the
private keys and the settings file that holds the Gemini key are owner-only
from their first byte; and the stop request is verified against Ninaivu's own
CA rather than waved through.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ninaivu.api.cloud_api import _endpoint_problem
from ninaivu.cloud import s3
from ninaivu.media import model_catalog
from ninaivu.server import stop
from ninaivu.utils import netscope, notify
from ninaivu.utils.notify import Notifier

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


# --- one definition of "never a target" ---------------------------------------

@pytest.mark.parametrize("host, where", [
    ("169.254.169.254", "never"), ("0.0.0.0", "never"), ("[fe80::1]", "never"),
    ("::ffff:169.254.1.1", "never"), ("224.0.0.1", "never"),
    ("192.168.1.20", "home"), ("10.0.0.2", "home"), ("127.0.0.1", "home"),
    ("100.101.102.103", "home"), ("fd00::5", "home"), ("localhost", "home"),
    ("comfy.local", "home"), ("nas.lan", "home"), ("den-pc", "home"),
    ("8.8.8.8", "public"), ("s3.example.com", "unknown"),
    ("metadata", "unknown"), ("metadata.google.internal", "unknown"),
])
def test_where_an_address_points(host, where):
    assert netscope.place(host) == where


# --- M3: the AI server ----------------------------------------------------------

@pytest.fixture()
def comfyui():
    return pytest.importorskip("ninaivu_studio.ai_server.comfyui")


@pytest.mark.parametrize("url", ["http://169.254.169.254", "http://169.254.169.254/latest",
                                 "http://0.0.0.0:8188", "http://[fe80::1]:8188",
                                 "http://metadata.google.internal", "http://metadata:8188"])
def test_the_ai_server_cannot_be_a_metadata_service(comfyui, url):
    """Photographs went to 169.254.169.254 over plain http, and up to 800
    characters of whatever answered came back on the console."""
    with pytest.raises(ValueError):
        comfyui.check_address(url)


@pytest.mark.parametrize("url", ["http://192.168.1.20:8188", "http://comfy.local:8188",
                                 "http://10.0.0.5:8188", "http://[fd00::5]:8188",
                                 "http://100.101.102.103:8188", "http://127.0.0.1:8188",
                                 "http://gpubox:8188", "https://comfy.internal"])
def test_a_comfyui_box_at_home_still_works(comfyui, url):
    assert comfyui.check_address(url) == url


def test_the_console_is_told_it_is_not_the_home_network(comfyui):
    assert comfyui.network_scope("http://169.254.169.254") == "public"
    assert comfyui.network_scope("http://metadata.google.internal") == "unknown"
    assert comfyui.network_scope("https://comfy.internal") == "home"


# --- L9: the webhook connects to the address it checked --------------------------

class _Handler(BaseHTTPRequestHandler):
    hosts: list[str] = []

    def do_POST(self):  # noqa: N802
        # Read what was posted before answering: closing with it unread
        # resets the connection under the client's feet on Windows.
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Handler.hosts.append(self.headers.get("Host", ""))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    _Handler.hosts = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port
    httpd.shutdown()


def _answer(address: str, port: int):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


def test_a_name_that_resolves_to_a_link_local_address_is_refused(monkeypatch):
    monkeypatch.setattr(notify.socket, "getaddrinfo",
                        lambda host, port, *a, **k: _answer("169.254.169.254", port))
    monkeypatch.setattr(notify.urllib.request.OpenerDirector, "open",
                        lambda *a, **k: pytest.fail("posted to a link-local address"))
    result = Notifier(webhook_url="http://hooks.example/x")._post("t", "d", "test")
    assert result == "The webhook cannot be a link-local address."


def test_the_post_goes_to_the_address_that_was_checked(monkeypatch, server):
    """A name that answers 127.0.0.1 to the check and 169.254.169.254 to the
    connection used to be checked on one and posted to the other."""
    answers = iter([_answer("127.0.0.1", server)])

    def rebinding(host, port, *args, **kwargs):
        return next(answers, _answer("169.254.169.254", port))

    monkeypatch.setattr(notify.socket, "getaddrinfo", rebinding)
    result = Notifier(webhook_url=f"http://hooks.example:{server}/x")._post("t", "d", "test")
    assert result == "ok"
    assert _Handler.hosts == [f"hooks.example:{server}"], "the name, not the address, is sent"


def test_an_address_that_does_not_answer_gives_way_to_the_next_one_checked(monkeypatch, server):
    """Pinning the connection to the checked address must not stop at the
    first: a name whose first answer is IPv6 on a house without an IPv6 route
    used to reach its IPv4 answer through urllib, and still must."""
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        closed = spare.getsockname()[1]
    monkeypatch.setattr(notify.socket, "getaddrinfo", lambda host, port, *a, **k:
                        _answer("127.0.0.1", closed) + _answer("127.0.0.1", server))
    result = Notifier(webhook_url=f"http://hooks.example:{server}/x")._post("t", "d", "test")
    assert result == "ok"
    assert _Handler.hosts == [f"hooks.example:{server}"]


def test_the_certificate_is_checked_against_the_name(server):
    """Pinning the address must not turn the https check into one against an
    IP: the socket goes to the address, the TLS handshake names the host."""
    seen = {}

    class Context:
        def wrap_socket(self, sock, server_hostname=None):
            seen["server_hostname"] = server_hostname
            seen["peer"] = sock.getpeername()
            return sock

    kind = type("Pinned", (notify._PinnedHTTPSConnection,),
                {"address": (socket.AF_INET, ("127.0.0.1", server))})
    connection = kind("hooks.example", 443, timeout=5, context=Context())
    connection.connect()
    connection.close()
    assert seen["server_hostname"] == "hooks.example"
    assert seen["peer"] == ("127.0.0.1", server)


def test_an_address_the_resolver_cannot_place_is_only_unreachable(monkeypatch):
    def nowhere(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(notify.socket, "getaddrinfo", nowhere)
    result = Notifier(webhook_url="http://hooks.example/x")._post("t", "d", "test")
    assert result == "Could not reach the webhook."


# --- L10: the off-site bucket ----------------------------------------------------

@pytest.mark.parametrize("endpoint", ["http://s3.example.com", "http://8.8.8.8:9000",
                                      "http://169.254.169.254", "s3.example.com",
                                      "ftp://s3.example.com", "http://"])
def test_a_plain_http_bucket_on_the_internet_is_refused(endpoint):
    assert _endpoint_problem(endpoint).startswith("The address starts with https://")


@pytest.mark.parametrize("endpoint", ["https://s3.example.com", "http://192.168.1.5:9000",
                                      "http://nas.local:9000", "http://127.0.0.1:9000",
                                      "https://s3.us-west-002.backblazeb2.com"])
def test_https_anywhere_and_http_at_home_are_accepted(endpoint):
    assert _endpoint_problem(endpoint) == ""


def test_what_the_service_said_is_shown_short_and_printable():
    """The <Message> came from whatever answered at the address, and the
    console's test button showed all of it, control characters included."""
    body = ("<Error><Code>AccessDenied</Code><Message>" + "a" * 500
            + "\r\n\tline two</Message></Error>").encode()

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    client = s3.S3(endpoint="https://s3.example.com", bucket="b", access_key="k",
                   secret_key="s", opener=opener)
    with pytest.raises(s3.S3Error) as caught:
        client.get("x")
    message = str(caught.value)
    assert message == "403: " + "a" * 200
    assert s3._error_in(body) == "a" * 200
    # An escape sequence cannot arrive inside XML (the parser refuses it) but
    # can in the status line, which is the fallback.
    assert s3._said("Forbidden\x1b[2J\r\nnow") == "Forbidden [2J now"


# --- L11: the private keys -------------------------------------------------------

def test_the_tls_folder_holds_what_it_always_did(tmp_path):
    pytest.importorskip("cryptography")
    from ninaivu.utils import tls

    cert, key = tls.ensure_certificate(tmp_path)
    names = {p.name for p in tls.tls_dir(tmp_path).iterdir()}
    assert names == {"ninaivu-ca.crt", "ninaivu-ca.key", "ninaivu.crt", "ninaivu.key",
                     "issued.json", tls.CONSTRAINTS_FILE}, "no temporary files left behind"
    assert Path(key).read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


@POSIX
def test_the_private_keys_are_owner_only_from_the_start(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from ninaivu.utils import tls

    # With the chmod afterwards out of the way, the mode is the one the files
    # were created with.
    monkeypatch.setattr(tls, "_private", lambda path: None)
    cert, key = tls.ensure_certificate(tmp_path)
    for path in (Path(key), tls.tls_dir(tmp_path) / "ninaivu-ca.key"):
        assert oct(os.stat(path).st_mode & 0o777) == "0o600", path
    assert oct(os.stat(tls.tls_dir(tmp_path)).st_mode & 0o777) == "0o700"


# --- L12: the settings file the Gemini key lives in ------------------------------

@pytest.fixture()
def models(tmp_path, monkeypatch):
    root = tmp_path / ".ai-models"
    monkeypatch.setattr(model_catalog, "models_root", lambda: root)
    return root


def test_the_settings_file_is_written_whole_and_tidily(models):
    model_id = next(m for m, model in model_catalog.MODELS.items() if model.get("settings"))
    model_catalog.apply_settings(model_id)
    target = model_catalog.settings_path()
    stored = json.loads(target.read_text())
    for name, relative in model_catalog.MODELS[model_id]["settings"].items():
        assert stored[name] == str(models / relative)
    assert not target.with_name(target.name + ".tmp").exists()


@POSIX
def test_the_settings_file_is_owner_only_before_any_key_is_in_it(models):
    model_id = next(m for m, model in model_catalog.MODELS.items() if model.get("settings"))
    model_catalog.apply_settings(model_id)
    target = model_catalog.settings_path()
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    # And stays so when the catalogue writes it again over an existing file.
    model_catalog.write_settings(target, {"gemini_api_key": "x"})
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_the_gemini_key_goes_through_the_same_writer(models, monkeypatch):
    gemini = pytest.importorskip("ninaivu_gemini.gemini")
    written = []
    monkeypatch.setattr(model_catalog, "write_settings",
                        lambda path, data: written.append((path, data)))
    gemini.save_api_key("AIzaSyD-example-key-1234567890abcdWXYZ")
    assert written and written[0][0] == model_catalog.settings_path()
    assert written[0][1]["gemini_api_key"].endswith("WXYZ")


# --- L14: the stop request is verified -------------------------------------------

def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_stop_request_is_verified_against_the_servers_own_ca(tmp_path, monkeypatch):
    import ssl

    (tmp_path / "tls").mkdir()
    (tmp_path / "tls" / "ninaivu-ca.crt").write_text("not read: the context is a stand-in")
    asked = []

    def default_context(*args, **kwargs):
        asked.append(kwargs.get("cafile"))
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(stop.ssl, "create_default_context", default_context)
    monkeypatch.setattr(stop.ssl, "_create_unverified_context",
                        lambda *a, **k: pytest.fail("the CA was there and was not used"))
    assert stop.ask_to_stop(_closed_port(), "token", timeout=1.0, scheme="https",
                            state_dir=tmp_path) is False
    assert asked == [str(tmp_path / "tls" / "ninaivu-ca.crt")]


def test_without_a_ca_of_its_own_the_request_is_unverified_as_before(tmp_path, monkeypatch):
    """A server given somebody else's certificate (--cert) has no CA file to
    check against; the request still never leaves the machine."""
    import ssl

    monkeypatch.setattr(stop.ssl, "create_default_context",
                        lambda *a, **k: pytest.fail("there is no CA to verify against"))
    unverified = []
    monkeypatch.setattr(stop.ssl, "_create_unverified_context",
                        lambda *a, **k: unverified.append(True) or ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    assert stop.ask_to_stop(_closed_port(), "token", timeout=1.0, scheme="https",
                            state_dir=tmp_path) is False
    assert unverified == [True]


def test_the_state_folder_is_found_as_the_server_finds_it(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path / "state"))
    assert stop.default_state_dir() == (tmp_path / "state").resolve()
    monkeypatch.delenv("NINAIVU_STATE_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert stop.default_state_dir() == (tmp_path / "xdg").resolve() / "ninaivu"


def test_the_stop_module_still_needs_nothing_but_the_standard_library():
    """tools/stop.py loads it by path on whatever Python is on PATH."""
    source = Path(stop.__file__).read_text(encoding="utf-8")
    assert "from ." not in source and "import ninaivu" not in source
    assert sys.version_info >= (3, 12)


# --- L15 and the launcher comment -------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_the_windows_installer_check_is_anchored_on_the_signers_name():
    script = (ROOT / "launcher" / "install-python.ps1").read_text(encoding="utf-8")
    assert "-notmatch '^CN=Python Software Foundation(,|$)'" in script
    assert "-notmatch 'Python Software Foundation'" not in script


def test_the_mac_installer_does_not_claim_an_update_check():
    script = (ROOT / "launcher" / "install-python-mac.sh").read_text(encoding="utf-8")
    assert "update check" not in script.replace("nothing here is an update check", "")
