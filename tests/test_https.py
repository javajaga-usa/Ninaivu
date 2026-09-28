"""Serving the house over HTTPS.

A bare self-signed certificate encrypts the connection and then greets every
member of the household with a full-page security warning, which teaches them
to click through security warnings. So Ninaivu issues its own CA, signs a
certificate with it, and serves the CA at a URL you can open on a phone. What
is tested here is that the certificate actually covers the addresses people
will type, that it can be installed without signing in first, and that it is
re-issued when it needs to be.
"""

import datetime
from pathlib import Path

import pytest

from conftest import ADMIN
from ninaivu.server import auth
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.utils import tls

cryptography = pytest.importorskip("cryptography")
from cryptography import x509                                    # noqa: E402


def load(path: Path):
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def san(cert) -> tuple[set[str], set[str]]:
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(ext.get_values_for_type(x509.DNSName)), {
        str(ip) for ip in ext.get_values_for_type(x509.IPAddress)}


# ---------------------------------------------------------------------------
# What gets issued
# ---------------------------------------------------------------------------

def test_a_certificate_and_a_ca_are_created(tmp_path):
    cert, key = tls.ensure_certificate(tmp_path)
    assert Path(cert).is_file() and Path(key).is_file()
    assert tls.ca_certificate_path(tmp_path).is_file()


def test_certificate_passes_strict_tls_handshake(tmp_path):
    import socket
    import ssl
    from concurrent.futures import ThreadPoolExecutor
    cert,key=tls.ensure_certificate(tmp_path)
    server=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server.load_cert_chain(cert,key)
    client=ssl.create_default_context(cafile=str(tls.ca_certificate_path(tmp_path)))
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
        def respond():
            connection,_=listener.accept()
            with server.wrap_socket(connection,server_side=True) as secured:
                secured.sendall(b'OK')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(respond)
            with socket.create_connection(listener.getsockname(),timeout=5) as connection:
                with client.wrap_socket(connection,server_hostname='localhost') as secured:
                    assert secured.recv(2)==b'OK'
            pending.result(timeout=5)


def test_legacy_certificate_profile_renews_without_replacing_ca(tmp_path):
    import json
    cert,_=tls.ensure_certificate(tmp_path)
    ca=tls.ca_certificate_path(tmp_path).read_bytes();old=cert.read_bytes()
    stamp=tls.tls_dir(tmp_path)/'issued.json'
    data=json.loads(stamp.read_text());data.pop('profile');stamp.write_text(json.dumps(data))
    tls.ensure_certificate(tmp_path)
    assert cert.read_bytes()!=old
    assert tls.ca_certificate_path(tmp_path).read_bytes()==ca


def test_the_certificate_covers_localhost_and_the_lan_address(tmp_path):
    """A certificate for localhost alone warns the moment anyone uses the LAN."""
    cert, _ = tls.ensure_certificate(tmp_path, extra_hosts=["192.168.1.24"])
    names, addresses = san(load(cert))

    assert "localhost" in names
    assert "127.0.0.1" in addresses
    assert "192.168.1.24" in addresses, "the address people actually type"


def test_extra_hostnames_are_honoured(tmp_path):
    cert, _ = tls.ensure_certificate(tmp_path, extra_hosts=["ninaivu.lan"])
    names, _ = san(load(cert))
    assert "ninaivu.lan" in names


def test_a_wildcard_bind_address_is_not_put_in_the_certificate(tmp_path):
    """`0.0.0.0` means "every interface", not a name anyone can connect to."""
    cert, _ = tls.ensure_certificate(tmp_path, extra_hosts=["0.0.0.0", "::"])
    names, addresses = san(load(cert))
    assert "0.0.0.0" not in addresses and "0.0.0.0" not in names


def test_the_leaf_is_short_lived_enough_for_apple_devices(tmp_path):
    """iOS and macOS reject server certificates valid for over ~398 days."""
    cert, _ = tls.ensure_certificate(tmp_path)
    loaded = load(cert)
    start = getattr(loaded, "not_valid_before_utc", None) or \
        loaded.not_valid_before.replace(tzinfo=datetime.timezone.utc)
    end = getattr(loaded, "not_valid_after_utc", None) or \
        loaded.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    assert (end - start).days <= 398


def test_the_certificate_is_signed_by_the_ca_not_by_itself(tmp_path):
    cert, _ = tls.ensure_certificate(tmp_path)
    leaf = load(cert)
    ca = load(tls.ca_certificate_path(tmp_path))
    assert leaf.issuer == ca.subject
    assert leaf.subject != leaf.issuer, "a plain self-signed cert is the thing we avoid"


def test_the_ca_is_a_ca_and_the_leaf_is_not(tmp_path):
    cert, _ = tls.ensure_certificate(tmp_path)
    ca_basic = load(tls.ca_certificate_path(tmp_path)).extensions \
        .get_extension_for_class(x509.BasicConstraints).value
    leaf_basic = load(cert).extensions \
        .get_extension_for_class(x509.BasicConstraints).value
    assert ca_basic.ca is True
    assert leaf_basic.ca is False


def test_the_served_file_is_the_full_chain(tmp_path):
    """Handing over the CA alongside the leaf saves a round of confusion."""
    cert, _ = tls.ensure_certificate(tmp_path)
    assert Path(cert).read_text().count("BEGIN CERTIFICATE") == 2


def test_the_private_keys_are_not_world_readable(tmp_path):
    import os
    import sys

    cert, key = tls.ensure_certificate(tmp_path)
    if sys.platform == "win32":
        pytest.skip("POSIX permissions do not apply")
    for path in (Path(key), tls.tls_dir(tmp_path) / "ninaivu-ca.key"):
        assert oct(os.stat(path).st_mode)[-3:] == "600", path


# ---------------------------------------------------------------------------
# Not re-issuing when it does not need to
# ---------------------------------------------------------------------------

def test_a_second_start_reuses_the_same_certificate(tmp_path):
    first, _ = tls.ensure_certificate(tmp_path)
    serial = load(first).serial_number
    second, _ = tls.ensure_certificate(tmp_path)
    assert load(second).serial_number == serial, "a new cert every start is churn"


def test_a_new_address_re_issues_the_certificate(tmp_path):
    """Move to another subnet and the old certificate no longer covers you."""
    first, _ = tls.ensure_certificate(tmp_path, extra_hosts=["192.168.1.24"])
    before = load(first).serial_number

    second, _ = tls.ensure_certificate(tmp_path, extra_hosts=["10.0.0.7"])
    after = load(second)
    assert after.serial_number != before
    assert "10.0.0.7" in san(after)[1]


def test_re_issuing_keeps_the_same_ca(tmp_path):
    """Otherwise every device in the house would have to install it again."""
    tls.ensure_certificate(tmp_path, extra_hosts=["192.168.1.24"])
    ca_before = load(tls.ca_certificate_path(tmp_path)).serial_number

    tls.ensure_certificate(tmp_path, extra_hosts=["10.0.0.7"])
    assert load(tls.ca_certificate_path(tmp_path)).serial_number == ca_before


def test_an_expiring_certificate_is_renewed(tmp_path):
    cert, _ = tls.ensure_certificate(tmp_path)
    before = load(cert).serial_number

    # Pretend the recorded issue is old by making the stamp disagree is not
    # enough — force the check that matters by shortening the renewal window.
    stamp = tls.tls_dir(tmp_path) / "issued.json"
    assert stamp.is_file()
    original = tls.RENEW_WITHIN_DAYS
    try:
        tls.RENEW_WITHIN_DAYS = tls.LEAF_DAYS + 1     # "always about to expire"
        again, _ = tls.ensure_certificate(tmp_path)
    finally:
        tls.RENEW_WITHIN_DAYS = original
    assert load(again).serial_number != before


# ---------------------------------------------------------------------------
# Installing it on a device
# ---------------------------------------------------------------------------

@pytest.fixture()
def apps(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    return create_home_app(services), create_admin_app(services), cfg


@pytest.mark.parametrize("path", ["/ninaivu-ca.crt", "/cert"])
def test_the_ca_can_be_downloaded_without_signing_in(apps, path):
    """A phone has to trust the server before it can present a login form."""
    home, _, cfg = apps
    tls.ensure_certificate(cfg.state_dir)

    response = home.test_client().get(path)
    assert response.status_code == 200
    assert response.mimetype == "application/x-x509-ca-cert"
    assert b"BEGIN CERTIFICATE" in response.get_data()


@pytest.mark.parametrize("path", ["/ninaivu-ca.crt", "/cert"])
def test_the_ca_download_works_on_a_private_library_too(apps, path):
    home, _, cfg = apps
    cfg.open_browsing = False
    tls.ensure_certificate(cfg.state_dir)
    assert home.test_client().get(path).status_code == 200


@pytest.mark.parametrize("path", ["/ninaivu-ca.crt", "/cert"])
def test_the_ca_download_works_on_the_console_before_signing_in(apps, path):
    _, admin, cfg = apps
    tls.ensure_certificate(cfg.state_dir)
    assert admin.test_client().get(path).status_code == 200


def test_the_download_is_offered_as_a_file(apps):
    """Phones install a certificate from a download, not from a rendered page."""
    home, _, cfg = apps
    tls.ensure_certificate(cfg.state_dir)
    disposition = home.test_client().get("/ninaivu-ca.crt").headers["Content-Disposition"]
    assert "attachment" in disposition and "ninaivu-ca.crt" in disposition


def test_no_ca_no_download(apps):
    """Behind a real certificate there is nothing of ours to install."""
    home, _, _ = apps
    response = home.test_client().get("/ninaivu-ca.crt")
    assert response.status_code == 404
    assert "not using a Ninaivu-issued certificate" in response.get_json()["error"]


def test_the_private_key_is_never_served(apps):
    home, _, cfg = apps
    tls.ensure_certificate(cfg.state_dir)
    body = home.test_client().get("/ninaivu-ca.crt").get_data()
    assert b"PRIVATE KEY" not in body


@pytest.mark.parametrize('path', ['/cert', '/ninaivu-ca.crt'])
def test_custom_tls_does_not_offer_a_stale_ninaivu_ca(apps, path):
    home, admin, cfg = apps
    tls.ensure_certificate(cfg.state_dir)
    for app in (home, admin):
        app.config['MV_SERVE_LOCAL_CA'] = False
        response = app.test_client().get(path)
        assert response.status_code == 404
        assert 'not using a Ninaivu-issued certificate' in response.json['error']


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def test_cert_without_key_is_refused(scanned, capsys):
    from ninaivu.__main__ import _resolve_tls, build_parser

    cfg, _, _ = scanned
    args = build_parser().parse_args(["--cert", "/tmp/nope.pem"])
    assert _resolve_tls(cfg, args) is False
    assert "must be given together" in capsys.readouterr().err


def test_a_missing_certificate_file_is_refused(scanned, capsys):
    from ninaivu.__main__ import _resolve_tls, build_parser

    cfg, _, _ = scanned
    args = build_parser().parse_args(
        ["--cert", "/tmp/nope.pem", "--key", "/tmp/nope.key"])
    assert _resolve_tls(cfg, args) is False
    assert "no such file" in capsys.readouterr().err


def test_your_own_certificate_skips_ninaivus_ca(scanned, tmp_path):
    """Behind a proxy or a real domain, none of the CA machinery should run."""
    from ninaivu.__main__ import _resolve_tls, build_parser

    cfg, _, _ = scanned
    mine_cert = tmp_path / "mine.pem"
    mine_key = tmp_path / "mine.key"
    mine_cert.write_text("x")
    mine_key.write_text("y")

    args = build_parser().parse_args(
        ["--cert", str(mine_cert), "--key", str(mine_key)])
    assert _resolve_tls(cfg, args) == (str(mine_cert), str(mine_key))
    assert not tls.tls_dir(cfg.state_dir).exists(), "no CA should have been made"


def test_plain_http_is_still_the_default(scanned):
    from ninaivu.__main__ import _resolve_tls, build_parser

    cfg, _, _ = scanned
    assert _resolve_tls(cfg, build_parser().parse_args([])) is None


def test_https_produces_a_usable_pair(scanned):
    from ninaivu.__main__ import _resolve_tls, build_parser

    cfg, _, _ = scanned
    resolved = _resolve_tls(cfg, build_parser().parse_args(["--https"]))
    assert resolved and len(resolved) == 2
    assert all(Path(p).is_file() for p in resolved)


# ---------------------------------------------------------------------------
# Finding the address to type on a phone
# ---------------------------------------------------------------------------

def test_a_real_lan_address_outranks_a_vpn_or_container_one():
    """The default route is not the answer on a machine running a VPN.

    Tailscale takes 100.64/10, Docker takes 172.17, and an adapter with no DHCP
    lease sits on 169.254 — none of which the family's phones can reach. A
    192.168 or 10.x address has to win.
    """
    ranked = sorted(
        ["100.101.5.7", "172.17.0.2", "169.254.9.9", "192.168.1.24", "10.0.0.5"],
        key=tls._rank)
    assert ranked[0] == "192.168.1.24"
    assert ranked[1] == "10.0.0.5"
    assert ranked[-1] == "169.254.9.9"
    assert ranked.index("100.101.5.7") > ranked.index("172.17.0.2")


def test_lan_addresses_never_offers_loopback():
    """Printing 127.0.0.1 as "open this on your phone" would be a cruel joke."""
    assert not any(a.startswith("127.") for a in tls.lan_addresses())


def test_lan_addresses_are_unique_and_ordered():
    found = tls.lan_addresses()
    assert len(found) == len(set(found))
    assert found == sorted(found, key=lambda a: (tls._rank(a), a))


def test_the_certificate_covers_every_lan_address(tmp_path):
    """Whichever address the phone ends up using has to be in the certificate.

    Every home-network address, that is: the CA is constrained to those, and a
    name outside its constraints would make strict clients reject the whole
    certificate, so an address like a container's 192.0.2.x is deliberately
    left off. A machine with such an address must see it left off, not fail.
    """
    cert, _ = tls.ensure_certificate(tmp_path)
    _, addresses = san(load(cert))
    constraints = tls._load_constraints(tls.tls_dir(tmp_path)) or tls._new_constraints()
    for address in tls.lan_addresses():
        if tls.permitted_address(address, constraints):
            assert address in addresses, address
        else:
            assert address not in addresses, f"{address} is outside the CA's constraints"


def test_the_banner_helper_agrees_with_the_ranked_list():
    from ninaivu.__main__ import lan_address, lan_addresses

    ranked = lan_addresses()
    assert lan_address() == (ranked[0] if ranked else None)
