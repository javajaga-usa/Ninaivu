"""Two computers when the Secondary's family app speaks HTTPS.

The Primary met it at http:// and was hung up on ("answers, but its family
app cannot be reached"); and the certificate, from the Secondary's own home
authority, was refused by the system's authorities even at https://. The
Secondary now says it speaks HTTPS and hands over its authority when found;
the number both screens show covers it; the Primary trusts that authority,
and only it, from then on.
"""
from __future__ import annotations

import json
import ssl
import threading
import time

import pytest
from werkzeug.serving import make_server

from conftest import login
from ninaivu.mugil import meeting, pairing
from ninaivu.mugil.courier import Partner, PartnerError
from ninaivu.utils import tls


def _ca(folder) -> str:
    tls.ensure_certificate(folder)
    return tls.ca_certificate_path(folder).read_text(encoding="ascii")


def test_the_number_covers_the_authority(tmp_path):
    _, primary = pairing.new_half()
    _, secondary = pairing.new_half()
    mine = pairing.ca_fingerprint(_ca(tmp_path / "a"))
    other = pairing.ca_fingerprint(_ca(tmp_path / "b"))
    assert len(mine) == 32 and mine != other
    plain = pairing.match_number(primary, secondary)
    assert pairing.match_number(primary, secondary, b"") == plain   # HTTP: as before
    assert pairing.match_number(primary, secondary, mine) != plain
    assert pairing.match_number(primary, secondary, mine) != \
        pairing.match_number(primary, secondary, other)
    assert pairing.ca_fingerprint("not a certificate") == b""
    assert pairing.ca_fingerprint(None) == b""


def test_a_secondary_on_https_says_so_when_found(cfg):
    cfg.mugil_role = "secondary"
    cfg.host = "0.0.0.0"
    ca = _ca(cfg.state_dir)
    welcome = meeting.Welcome()
    welcome.open()
    beacon = meeting.Beacon(cfg, welcome, lambda: "Linux PC")
    beacon.https, beacon.own_ca = True, True
    beacon.keep()
    try:
        found = []
        for _ in range(20):
            found = meeting.discover(listen=0.5, targets=["127.0.0.1"])
            if found:
                break
        assert found and found[0]["https"] is True
        assert found[0]["ca"] == ca
        beacon.own_ca = False                       # its own certificate, given with --cert
        time.sleep(0.2)
        again = meeting.discover(listen=0.5, targets=["127.0.0.1"])
        assert again and again[0]["https"] is True and again[0]["ca"] is None
    finally:
        beacon.stop(join=True)


@pytest.fixture()
def https_server(tmp_path):
    """A server with a certificate from its own home authority, as a
    Ninaivu with Use HTTPS on has."""
    from flask import Flask, jsonify
    folder = tmp_path / "secondary"
    cert, key = tls.ensure_certificate(folder)
    app = Flask(__name__)

    @app.get("/api/mugil/hello")
    def hello():
        return jsonify({"role": "secondary"})

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server = make_server("127.0.0.1", 0, app, ssl_context=context, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", \
            tls.ca_certificate_path(folder).read_text(encoding="ascii")
    finally:
        server.shutdown()


def test_the_primary_trusts_the_authority_it_paired_with_and_only_it(https_server, tmp_path):
    url, ca = https_server
    assert Partner(url, ca=ca).request("GET", "/api/mugil/hello", signed=False) == \
        {"role": "secondary"}
    # The system's authorities do not know it: refused, as before.
    with pytest.raises(PartnerError):
        Partner(url).request("GET", "/api/mugil/hello", signed=False)
    # Another computer's authority is not it.
    with pytest.raises(PartnerError):
        Partner(url, ca=_ca(tmp_path / "someone-else")).request(
            "GET", "/api/mugil/hello", signed=False)


def test_the_pairing_remembers_the_authority(https_server, tmp_path):
    url, ca = https_server
    state = tmp_path / "primary-state"
    state.mkdir()
    pairing.save(state, {"key": "ab" * 32, "role": "primary", "partner_url": url,
                         "partner_ca": ca})
    loaded = pairing.load(state)
    assert "partner_ca" not in pairing.public(loaded)
    # Every Partner made from the pairing's key uses it.
    assert Partner(loaded["partner_url"], loaded["key"]).ca == ca
    assert Partner(url, "cd" * 32).ca is None


@pytest.fixture()
def two_apps_https(tmp_path, cfg, scanned, monkeypatch):
    """A Secondary serving HTTPS with its own authority, and a Primary."""
    from conftest import ADMIN
    from ninaivu import create_app
    from ninaivu.api import mugil_api
    from ninaivu.server import auth
    from ninaivu.server.config import Config
    from ninaivu.storage import db
    from test_mugil_two_computers import partner_through

    other = Config()
    other.state_dir = tmp_path / "secondary-state"
    other.roots = []
    other.active_root = ""
    other.ai_enabled, other.ai_engine, other.watch = False, "off", False
    other.ai_models_dir = str(tmp_path / "ai-models")
    other.ensure_dirs()
    ca = _ca(other.state_dir)
    conn = db.init_db(other.db_path)
    auth.init_auth_schema(conn)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    secondary_app = create_app(other)
    secondary_app.config["MV_HTTPS"] = True
    secondary_app.config["MV_SERVE_LOCAL_CA"] = True

    auth.bootstrap_admin(scanned[1], ADMIN[0], ADMIN[1], "Dad")
    primary_app = create_app(cfg)
    primary_app.config["MV_SCANNER"].stop(join=True)

    made = []
    through = partner_through(secondary_app.test_client())

    class Seen(through):
        def __init__(self, url, *args, **kwargs):
            super().__init__(url, *args, **kwargs)
            made.append((url, self.ca))

    monkeypatch.setattr(mugil_api, "Partner", Seen)
    monkeypatch.setattr(meeting, "discover", lambda **_: [
        {"id": "linux-pc-id", "address": "192.168.1.30", "port": 8443, "name": "Linux PC",
         "waiting": True, "https": True, "ca": ca}])
    yield primary_app, secondary_app, other, ca, made
    for application in (primary_app, secondary_app):
        application.config["MV_SERVICES"].stop(timeout=5.0)


def test_a_secondary_on_https_pairs_with_one_tap(two_apps_https, tmp_path):
    from conftest import ADMIN
    primary_app, secondary_app, other, ca, made = two_apps_https
    second = login(secondary_app.test_client(), *ADMIN)
    first = login(primary_app.test_client(), *ADMIN)
    folder = tmp_path / "n300"
    folder.mkdir()
    assert second.post("/api/admin/mugil/role", json={
        "role": "secondary", "originals_dir": str(folder)}).status_code == 200
    assert first.post("/api/admin/mugil/role", json={"role": "primary"}).status_code == 200

    found = first.post("/api/admin/mugil/look").get_json()["found"]
    assert found[0].get("unreachable") is not True and found[0]["number"]
    assert made[0] == ("https://192.168.1.30:8443", ca)
    shown = second.get("/api/admin/mugil").get_json()["welcome"]["introduced"]
    assert [i["number"] for i in shown] == [found[0]["number"]]

    assert first.post("/api/admin/mugil/pair", json={"found": "linux-pc-id"}).status_code == 200
    saved = pairing.load(primary_app.config["MV_CONFIG"].state_dir)
    assert saved["partner_url"] == "https://192.168.1.30:8443"
    assert saved["partner_ca"] == ca
    assert "partner_ca" not in json.dumps(first.get("/api/admin/mugil").get_json())


def test_an_authority_swapped_on_the_way_shows_another_number(two_apps_https, tmp_path,
                                                              monkeypatch):
    from conftest import ADMIN
    primary_app, secondary_app, other, ca, made = two_apps_https
    second = login(secondary_app.test_client(), *ADMIN)
    first = login(primary_app.test_client(), *ADMIN)
    folder = tmp_path / "n300"
    folder.mkdir()
    second.post("/api/admin/mugil/role", json={"role": "secondary", "originals_dir": str(folder)})
    first.post("/api/admin/mugil/role", json={"role": "primary"})
    swapped = _ca(tmp_path / "in-between")
    monkeypatch.setattr(meeting, "discover", lambda **_: [
        {"id": "linux-pc-id", "address": "192.168.1.30", "port": 8443, "name": "Linux PC",
         "waiting": True, "https": True, "ca": swapped}])
    found = first.post("/api/admin/mugil/look").get_json()["found"]
    shown = second.get("/api/admin/mugil").get_json()["welcome"]["introduced"]
    assert [i["number"] for i in shown] != [found[0]["number"]]


def test_joining_with_the_code_carries_the_authority_in_the_proof(tmp_path):
    ca = _ca(tmp_path / "secondary")
    codes = pairing.Codes()
    code = codes.new()
    private, hello = pairing.primary_hello(code, ca)
    assert hello["ca"] == ca
    # Swapped on the way, the proof no longer holds.
    tampered = {**hello, "ca": _ca(tmp_path / "in-between")}
    assert pairing.secondary_answer(codes, tampered) is None
    assert pairing.secondary_answer(codes, {k: v for k, v in hello.items() if k != "ca"}) is None
    found = pairing.secondary_answer(codes, hello)
    assert found is not None
    key, answer = found
    assert pairing.primary_key(private, code, answer) == key
