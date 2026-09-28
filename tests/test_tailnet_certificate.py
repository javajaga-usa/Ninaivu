"""Ninaivu answers its Tailscale name with Tailscale's certificate.

A phone on the tailnet reached Ninaivu at <computer>.<tailnet>.ts.net and was
shown Ninaivu's own certificate, made for the house's names, and refused it.
Tailscale is not driven here; a stand-in answers its commands.
"""
import datetime
import json
import os
import socket
import ssl
import subprocess
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ninaivu.utils import tailnet

NAME = "jagas-mac-mini.tail07dcbf.ts.net"


def certificate(name):
    """A self-signed certificate and key for *name*, as PEM text."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()).decode())


class Tailscale:
    def __init__(self, *, https=True, running=True, cert=None):
        self.https, self.running = https, running
        self.cert = cert or certificate(NAME)
        self.commands = []

    def __call__(self, command):
        self.commands.append(command[1:])
        if command[1:3] == ["status", "--json"]:
            body = {"BackendState": "Running" if self.running else "NeedsLogin",
                    "Self": {"DNSName": NAME + "."},
                    "CertDomains": [NAME] if self.https else None}
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
        if command[1] == "cert":
            chain, key = self.cert
            return subprocess.CompletedProcess(command, 0, chain + key, "")
        return subprocess.CompletedProcess(command, 1, "", "unknown")


def holder(tmp_path, tailscale):
    return tailnet.TailnetCertificate(tmp_path / "tls", runner=tailscale, cli=lambda: "tailscale")


def test_the_certificate_is_fetched_kept_and_used_for_its_name(tmp_path):
    tailscale = Tailscale()
    cert = holder(tmp_path, tailscale)
    assert cert.refresh() is True
    assert cert.name == NAME and cert.context is not None
    assert ["cert", "--cert-file=-", "--key-file=-", NAME] in tailscale.commands
    crt, key, _ = cert.files
    assert crt.read_text().startswith("-----BEGIN CERTIFICATE-----")
    if os.name != "nt":
        assert (key.stat().st_mode & 0o777) == 0o600, "the key is this account's alone"


def test_the_copy_from_last_time_is_used_before_tailscale_is_asked(tmp_path):
    holder(tmp_path, Tailscale()).refresh()
    later = tailnet.TailnetCertificate(tmp_path / "tls", runner=lambda c: pytest.fail("asked"),
                                       cli=lambda: "tailscale")
    assert later.load_saved() is True and later.name == NAME


def test_without_https_certificates_nothing_is_fetched(tmp_path):
    tailscale = Tailscale(https=False)
    cert = holder(tmp_path, tailscale)
    assert cert.refresh() is False and cert.context is None
    assert not any(c[0] == "cert" for c in tailscale.commands)


def test_https_turned_off_later_stops_using_it(tmp_path):
    tailscale = Tailscale()
    cert = holder(tmp_path, tailscale)
    cert.refresh()
    tailscale.https = False
    assert cert.refresh() is False and cert.context is None


def test_not_connected_or_not_installed_changes_nothing(tmp_path):
    assert holder(tmp_path, Tailscale(running=False)).refresh() is False
    absent = tailnet.TailnetCertificate(tmp_path / "tls", runner=Tailscale(), cli=lambda: None)
    assert absent.refresh() is False


def test_a_refusal_keeps_the_certificate_already_held(tmp_path):
    tailscale = Tailscale()
    cert = holder(tmp_path, tailscale)
    cert.refresh()
    tailscale.cert = ("", "")
    assert cert.refresh() is True and cert.name == NAME


def test_the_status_is_read():
    assert tailnet.this_computer("t", Tailscale()) == {"name": NAME, "https": True}
    assert tailnet.this_computer("t", Tailscale(https=False))["https"] is False
    assert tailnet.this_computer("t", Tailscale(running=False)) is None


def test_the_cli_is_found_inside_the_mac_app():
    app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    assert tailnet.find_cli("darwin", which=lambda _n: None, exists=lambda p: p == app) == app
    assert tailnet.find_cli("linux", which=lambda _n: None, exists=lambda _p: True) is None


def test_the_choice_never_fails_a_handshake():
    cert = tailnet.TailnetCertificate("/nowhere")
    cert.name, cert.context = NAME, object()
    cert.choose(None, NAME, None)            # no socket to set it on: ignored


# -- a real handshake ---------------------------------------------------------

def served_certificate(server_context, asked_for):
    """The subject a client asking for *asked_for* is shown."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def answer():
        conn, _ = listener.accept()
        try:
            with server_context.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    client = ssl.create_default_context()
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with client.wrap_socket(raw, server_hostname=asked_for) as tls:
            der = tls.getpeercert(binary_form=True)
    thread.join(5)
    listener.close()
    name = x509.load_der_x509_certificate(der).subject
    return name.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def test_the_tailnet_name_gets_tailscales_certificate_and_the_house_ninaivus(tmp_path):
    ninaivu_crt, ninaivu_key = certificate("localhost")
    (tmp_path / "ninaivu.crt").write_text(ninaivu_crt)
    (tmp_path / "ninaivu.key").write_text(ninaivu_key)
    cert = holder(tmp_path, Tailscale())
    cert.refresh()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tmp_path / "ninaivu.crt"), str(tmp_path / "ninaivu.key"))
    cert.attach(context)

    assert served_certificate(context, NAME) == NAME
    assert served_certificate(context, NAME.upper() + ".") == NAME
    assert served_certificate(context, "ninaivu.local") == "localhost"
