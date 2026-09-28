"""HTTPS for a home network.

The awkward thing about TLS on a LAN is that there is no certificate authority
willing to vouch for ``192.168.1.24``. The usual answer — a bare self-signed
certificate — technically encrypts the connection but greets everyone in the
house with a full-page security warning, and teaches them to click through it.
That is a worse outcome than plain HTTP.

So Ninaivu does what a developer would do for themselves: it creates its own
small certificate authority, keeps the private key on this machine, and uses it
to sign a certificate for this server. Install the CA once per device — the
console tells you how, and the file is served at ``/ninaivu-ca.crt`` — and every
browser in the house gets an ordinary padlock with no warning at all.

Three practical decisions worth knowing about:

**The CA is created once and never rotated.** Every device that trusts it keeps
trusting it. Losing the CA key would mean re-installing on every device, so it
lives in the state directory alongside everything else Ninaivu owns.

**The server certificate lasts 397 days and is renewed automatically.** Apple
platforms reject server certificates valid for longer than that, so a "ten year
cert, set and forget" would simply not work on the family's phones. Ninaivu
re-issues it when it is within a month of expiry; because the CA is unchanged,
nothing needs re-installing.

**The certificate is re-issued when the address set changes.** Move the machine
to a new subnet and its old certificate no longer names the address people are
typing, so it is quietly replaced.

If you already have a real certificate — from a reverse proxy, from mkcert, or
from Let's Encrypt via a domain you own — pass ``--cert`` and ``--key`` and none
of this runs.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import socket
import subprocess
from pathlib import Path

__all__ = [
    "ensure_certificate", "ca_certificate_path", "tls_dir",
    "describe", "CertificateUnavailable", "lan_addresses",
]

#: Apple platforms refuse a server certificate valid for longer than this, so a
#: longer one would work everywhere except the phones people actually use.
LEAF_DAYS = 397
#: Re-issue when the leaf has less than this left, on any start.
RENEW_WITHIN_DAYS = 30
#: The CA outlives many leaf certificates; installed once per device.
CA_YEARS = 10

#: What a Ninaivu CA may vouch for, written into it as name constraints.
#:
#: A device told to trust this CA would otherwise accept a certificate it
#: signed for *any* site — a bank, a mail provider — and the key that signs
#: them sits unencrypted in the state directory and in every backup. Held to
#: home-network names and private addresses, a stolen key can impersonate
#: nothing but Ninaivu-shaped hosts. The machine's own host name is added when
#: the CA is made, because people do type ``https://den-pc``.
PERMITTED_DNS = ("local", "home.arpa", "lan", "internal", "localhost")
PERMITTED_NETWORKS = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "169.254.0.0/16", "100.64.0.0/10",                  # link-local, CGNAT/Tailscale
    "::1/128", "fc00::/7", "fe80::/10",
)
#: Where the constraints a CA was made with are remembered. A CA made before
#: constraints existed has no such file, and its leaves are left unfiltered
#: rather than suddenly dropping names the household already relies on.
CONSTRAINTS_FILE = "ca-constraints.json"


def _new_constraints() -> dict[str, list[str]]:
    dns = list(PERMITTED_DNS)
    for name in local_names():
        name = name.lower()
        if "." not in name and name.isascii() and name not in dns:
            dns.append(name)
    return {"dns": dns, "ip": list(PERMITTED_NETWORKS)}


def _load_constraints(directory: Path) -> dict[str, list[str]] | None:
    try:
        data = json.loads((directory / CONSTRAINTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {"dns": [str(d) for d in data.get("dns", [])],
            "ip": [str(n) for n in data.get("ip", [])]}


def permitted_name(name: str, constraints: dict[str, list[str]]) -> bool:
    name = name.lower().rstrip(".")
    return any(name == base or name.endswith("." + base) for base in constraints["dns"])


def permitted_address(address: str, constraints: dict[str, list[str]]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip.version == net.version and ip in net
               for net in (ipaddress.ip_network(n) for n in constraints["ip"]))


class CertificateUnavailable(RuntimeError):
    """No way to create a certificate on this machine."""


def tls_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "tls"


def ca_certificate_path(state_dir: Path | str) -> Path:
    """The file to install on phones, tablets and other computers."""
    return tls_dir(state_dir) / "ninaivu-ca.crt"


# ---------------------------------------------------------------------------
# What the certificate has to cover
# ---------------------------------------------------------------------------

def _probe(target: str) -> str | None:
    """Which of our addresses the OS would use to reach *target*.

    No packet is sent — connecting a UDP socket only fixes the route. Probing
    a target *inside* each private block is the trick that finds the LAN
    adapter even when a VPN owns the default route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target, 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _rank(address: str) -> int:
    """Lower sorts first. How likely is this the address to type on a phone?"""
    if address.startswith("192.168."):
        return 0                                 # the overwhelmingly common case
    if address.startswith("10."):
        return 1
    try:
        second = int(address.split(".")[1])
    except (IndexError, ValueError):
        return 9
    if address.startswith("172.") and 16 <= second <= 31:
        # Docker's default bridge lives at 172.17, and is not reachable from
        # anyone else's phone.
        return 6 if second == 17 else 2
    if address.startswith("169.254."):
        return 8                                 # link-local: no DHCP happened
    if address.startswith("100.") and 64 <= second <= 127:
        return 7                                 # CGNAT — this is Tailscale's range
    return 3


def lan_addresses() -> list[str]:
    """Every address of this machine another device might reach it on.

    Returns the most likely first. A single answer is not good enough in
    practice: a Windows box with Tailscale, VirtualBox, Hyper-V or WSL running
    has several addresses, and the one on the default route is often the one
    nobody else can reach.
    """
    found: list[str] = []

    def note(address: str | None) -> None:
        if address and address not in found and not address.startswith("127."):
            found.append(address)

    # The default route first, then one probe per private block — each reveals
    # the adapter that block is reached through, if any.
    for target in ("192.0.2.1", "192.168.0.1", "192.168.1.1",
                   "10.0.0.1", "172.16.0.1"):
        note(_probe(target))

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            note(info[4][0])
    except (socket.gaierror, OSError):
        pass

    ordered = sorted(found, key=lambda a: (_rank(a), a))

    # Hyper-V hands out 192.168.176.1 and WSL 172.29.x — addresses that look
    # exactly as much like "the home network" as the real Wi-Fi one does, and
    # a phone told to use one waits until it times out rather than failing.
    # The adapter that has a *default gateway* is the one the phone is on, so
    # when the OS will tell us, that answer comes first.
    try:
        from . import netinfo

        routed = [a for a in netinfo.gateway_addresses() if a in ordered]
    except Exception:                            # noqa: BLE001 — advisory only
        routed = []
    if routed:
        ordered = routed + [a for a in ordered if a not in routed]
    return ordered


def local_addresses() -> list[str]:
    """Addresses worth writing into a certificate: loopback plus every LAN one.

    A certificate is only valid for the names and addresses written into it, so
    one covering ``localhost`` alone produces a warning the moment somebody
    types the LAN address — which is the entire point of serving the house.
    """
    unique = ["127.0.0.1", "::1"]
    for address in lan_addresses():
        if address not in unique:
            unique.append(address)

    valid: list[str] = []
    for address in unique:
        try:
            ipaddress.ip_address(address)
        except ValueError:
            continue
        if address not in valid:
            valid.append(address)
    return valid


def local_names() -> list[str]:
    names = ["localhost"]
    try:
        host = socket.gethostname()
    except OSError:
        return names
    short = host.split(".")[0]
    for candidate in (host, f"{host}.local", short, f"{short}.local"):
        # A Mac's hostname already carries .local ("Family-Mac-Mini.local"), and
        # adding another put "Family-Mac-Mini.local.local" in the certificate.
        if (candidate and not candidate.lower().endswith(".local.local")
                and candidate not in names):
            names.append(candidate)
    return names


def _subject_set(extra_hosts) -> tuple[list[str], list[str]]:
    names = list(local_names())
    addresses = list(local_addresses())
    for host in extra_hosts or ():
        host = str(host).strip()
        if not host or host in ("0.0.0.0", "::"):
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host not in names:
                names.append(host)
        else:
            if host not in addresses:
                addresses.append(host)
    return names, addresses


# ---------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------

def ensure_certificate(state_dir: Path | str,
                       extra_hosts=()) -> tuple[Path, Path]:
    """Return ``(certfile, keyfile)`` for this machine, creating them if needed.

    Cheap and idempotent: on a normal start it verifies the existing
    certificate still covers the right addresses and is not about to expire,
    and does nothing else.
    """
    directory = tls_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _lock_down(directory)

    cert = directory / "ninaivu.crt"
    key = directory / "ninaivu.key"
    stamp = directory / "issued.json"

    names, addresses = _subject_set(extra_hosts)
    have_ca = (directory / "ninaivu-ca.crt").is_file() and (directory / "ninaivu-ca.key").is_file()
    if not have_ca:
        (directory / CONSTRAINTS_FILE).write_text(
            json.dumps(_new_constraints(), indent=1), encoding="utf-8")
    constraints = _load_constraints(directory)
    if constraints is not None:
        # A name outside the CA's constraints would make strict clients reject
        # the whole certificate, not just that name, so it is left off.
        dropped = ([n for n in names if not permitted_name(n, constraints)]
                   + [a for a in addresses if not permitted_address(a, constraints)])
        if dropped:
            import logging
            # A CA covers the bare name the computer had when it was made. Moved
            # to a Mac with the library, a Windows PC's CA does not cover
            # "Family-Mac-Mini", dropped at every start: as a warning, a problem
            # on the console's list that nobody could do anything about.
            bare = [n for n in dropped if n in names and "." not in n]
            others = [n for n in dropped if n not in bare]
            if bare:
                logging.getLogger(__name__).info(
                    "Ninaivu's certificate covers %s, not %s: its CA was made before "
                    "this computer had that name", ", ".join(f"{n}.local" for n in bare),
                    ", ".join(bare))
            if others:
                logging.getLogger(__name__).warning(
                    "not putting %s in Ninaivu's certificate: its CA only covers "
                    "home-network names and private addresses", ", ".join(others))
        names = [n for n in names if permitted_name(n, constraints)]
        addresses = [a for a in addresses if permitted_address(a, constraints)]
    wanted = {"names": sorted(names), "addresses": sorted(addresses), "profile": 2}

    if cert.is_file() and key.is_file() and _still_good(stamp, wanted, cert):
        return cert, key

    try:
        _issue_with_cryptography(directory, names, addresses)
    except ImportError:
        _issue_with_openssl(directory, names, addresses)

    stamp.write_text(json.dumps(wanted, indent=1))
    for path in (key, directory / "ninaivu-ca.key"):
        _private(path)
    return cert, key


def _still_good(stamp: Path, wanted: dict, cert: Path) -> bool:
    try:
        if json.loads(stamp.read_text()) != wanted:
            return False
    except (OSError, ValueError):
        return False
    remaining = _days_left(cert)
    return remaining is None or remaining > RENEW_WITHIN_DAYS


def _days_left(cert: Path) -> int | None:
    try:
        from cryptography import x509
    except ImportError:
        return None
    try:
        loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    except (OSError, ValueError):
        return 0
    expires = getattr(loaded, "not_valid_after_utc", None)
    if expires is None:                          # cryptography < 42
        expires = loaded.not_valid_after.replace(tzinfo=_dt.timezone.utc)
    return (expires - _dt.datetime.now(_dt.timezone.utc)).days


def _issue_with_cryptography(directory: Path, names, addresses) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)

    ca_cert_path = directory / "ninaivu-ca.crt"
    ca_key_path = directory / "ninaivu-ca.key"

    if ca_cert_path.is_file() and ca_key_path.is_file():
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), None)
    else:
        # RSA rather than an elliptic curve: smart TVs and older tablets are
        # the devices most likely to be on a household network and the least
        # likely to have kept up.
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Ninaivu local CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ninaivu"),
        ])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + _dt.timedelta(days=365 * CA_YEARS))
        )
        constraints = _load_constraints(directory)
        if constraints is not None:
            # Not marked critical: an old television that cannot read the
            # extension should still get its padlock, and every current
            # browser and operating system enforces it either way.
            builder = builder.add_extension(x509.NameConstraints(
                permitted_subtrees=(
                    [x509.DNSName(d) for d in constraints["dns"]]
                    + [x509.IPAddress(ipaddress.ip_network(n)) for n in constraints["ip"]]),
                excluded_subtrees=None), critical=False)
        ca_cert = (
            builder
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        ca_key_path.write_bytes(ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alt: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    alt += [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]

    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, names[0]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ninaivu"),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
        .add_extension(x509.ExtendedKeyUsage([
            x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=True,
            content_commitment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    (directory / "ninaivu.crt").write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM))     # full chain
    (directory / "ninaivu.key").write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))


def _issue_with_openssl(directory: Path, names, addresses) -> None:
    """Fallback for a Python without ``cryptography`` but with the CLI."""
    import shutil

    openssl = shutil.which("openssl")
    if not openssl:
        raise CertificateUnavailable(
            "Ninaivu needs either the 'cryptography' package or the 'openssl' "
            "command to create an HTTPS certificate. Install one with "
            "'pip install cryptography', or supply your own certificate with "
            "--cert and --key."
        )

    san = ",".join([f"DNS:{n}" for n in names] + [f"IP:{a}" for a in addresses])
    ca_cert = directory / "ninaivu-ca.crt"
    ca_key = directory / "ninaivu-ca.key"

    def run(*args: str) -> None:
        result = subprocess.run([openssl, *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise CertificateUnavailable(
                f"openssl failed: {result.stderr.strip() or result.stdout.strip()}")

    if not (ca_cert.is_file() and ca_key.is_file()):
        extra: list[str] = []
        constraints = _load_constraints(directory)
        if constraints is not None:
            subtrees = [f"permitted;DNS:{d}" for d in constraints["dns"]]
            for n in constraints["ip"]:
                network = ipaddress.ip_network(n)
                subtrees.append(f"permitted;IP:{network.network_address}/{network.netmask}")
            extra = ["-addext", "nameConstraints=" + ",".join(subtrees)]
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(ca_key), "-out", str(ca_cert),
            "-days", str(365 * CA_YEARS),
            "-subj", "/O=Ninaivu/CN=Ninaivu local CA",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign,digitalSignature",
            *extra)

    leaf_key = directory / "ninaivu.key"
    csr = directory / "ninaivu.csr"
    leaf = directory / "ninaivu-leaf.crt"
    extensions = directory / "ninaivu-ext.cnf"
    extensions.write_text(
        f"subjectAltName={san}\n"
        "basicConstraints=critical,CA:FALSE\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "extendedKeyUsage=serverAuth\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
    )
    try:
        run("req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(leaf_key), "-out", str(csr),
            "-subj", f"/O=Ninaivu/CN={names[0]}")
        run("x509", "-req", "-in", str(csr),
            "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(leaf), "-days", str(LEAF_DAYS), "-sha256",
            "-extfile", str(extensions))
        (directory / "ninaivu.crt").write_bytes(
            leaf.read_bytes() + ca_cert.read_bytes())
    finally:
        for temporary in (csr, leaf, extensions):
            temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------

def _lock_down(directory: Path) -> None:
    try:
        directory.chmod(0o700)
    except OSError:
        pass                                     # Windows, or a odd filesystem


def _private(path: Path) -> None:
    try:
        if path.is_file():
            path.chmod(0o600)
    except OSError:
        pass


def describe(cert: Path) -> str:
    """One line about a certificate, for the startup banner."""
    days = _days_left(Path(cert))
    if days is None:
        return "certificate in use"
    if days <= 0:
        return "certificate has expired"
    return f"certificate valid for {days} more days"


# ---------------------------------------------------------------------------
# Trusting the CA on this Windows machine
# ---------------------------------------------------------------------------

def ca_thumbprint(cert: Path | str) -> str | None:
    """The certificate's SHA-1 thumbprint, as Windows' certificate stores name it."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        certificate = x509.load_pem_x509_certificate(Path(cert).read_bytes())
        return certificate.fingerprint(hashes.SHA1()).hex().upper()
    except Exception:  # noqa: BLE001 - no cryptography, or not a certificate
        return None


def windows_store_holds(store: str, thumbprint: str, runner=subprocess.run) -> bool:
    """Whether the current user's *store* ("Root", "CA") holds the certificate."""
    done = runner(["certutil.exe", "-user", "-store", store, thumbprint],
                  capture_output=True, text=True, check=False)
    return done.returncode == 0


def trust_ca_on_mac(cert: Path | str, runner=subprocess.run) -> tuple[bool, str]:
    """Trust Ninaivu's CA for websites in this Mac's login keychain, and check it.

    Importing it is the step that goes wrong. Keychain Access's import offers
    the iCloud or Local Items keychain, which take no certificates, and a
    certificate already imported cannot be imported again. Both fail with
    "unable to import", and neither marks it trusted, which is the step that
    matters. ``security add-trusted-cert`` adds it if it is missing and trusts
    it for SSL only, not code signing or mail. macOS asks for the person's
    password in its own window, so nothing here is silent. Success is judged by
    whether this Ninaivu's own certificate verifies afterwards, not by the
    command's word for it. Returns (trusted, message).
    """
    thumbprint = ca_thumbprint(cert)
    if thumbprint is None:
        return False, "Ninaivu's certificate could not be read, so it was not trusted."
    cert = Path(cert)
    server = cert.with_name("ninaivu.crt")
    if _mac_verifies(server if server.is_file() else cert, runner):
        message = "This Mac already trusts Ninaivu's certificate."
    else:
        keychain = Path.home() / "Library" / "Keychains" / "login.keychain-db"
        added = runner(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl",
                        "-k", str(keychain), str(cert)],
                       capture_output=True, text=True, check=False)
        if added.returncode != 0 or not _mac_verifies(server if server.is_file() else cert,
                                                      runner):
            return False, ("macOS did not trust the certificate (the password window may "
                           "have been cancelled).")
        message = ("Trusted for websites in this Mac's login keychain. Restart the browser "
                   "so it picks the certificate up.")
    others = _mac_namesakes(thumbprint, runner)
    if others:
        message += (f" {others} other certificate{'s' if others > 1 else ''} called "
                    "\"Ninaivu local CA\" — from another Ninaivu or an earlier setup — "
                    f"{'are' if others > 1 else 'is'} also in the keychains. "
                    f"{'They do' if others > 1 else 'It does'} nothing for this one, and "
                    "can be deleted in Keychain Access if you prefer.")
    return True, message


def _mac_verifies(cert: Path, runner) -> bool:
    """Whether macOS accepts *cert* for a website, which is what trust is for."""
    result = runner(["security", "verify-cert", "-c", str(cert), "-p", "ssl",
                     "-s", "localhost"], capture_output=True, text=True, check=False)
    return result.returncode == 0


def _mac_namesakes(thumbprint: str, runner) -> int:
    """Other certificates in the login and System keychains with Ninaivu's CA name."""
    found: set[str] = set()
    for keychain in (Path.home() / "Library" / "Keychains" / "login.keychain-db",
                     Path("/Library/Keychains/System.keychain")):
        result = runner(["security", "find-certificate", "-a", "-c", "Ninaivu local CA",
                         "-Z", str(keychain)], capture_output=True, text=True, check=False)
        for line in (result.stdout or "").splitlines():
            if line.startswith("SHA-1 hash:"):
                found.add(line.split(":", 1)[1].strip().upper())
    found.discard(thumbprint)
    return len(found)


def trust_ca_on_windows(cert: Path | str, runner=subprocess.run) -> tuple[bool, str]:
    """Add Ninaivu's CA to this Windows account's Trusted Root store, and check it.

    Windows' certificate import window defaults to "Automatically select the
    certificate store", which files a home-made certificate authority under
    Intermediate Certification Authorities -- where no browser trusts it -- so
    the security warning stays after what looked like a successful install.
    Adding it to Root directly, as tools/enable-local-https.ps1 always has,
    cannot land in the wrong place. Windows asks the person to confirm adding a
    root certificate, so nothing here is silent. Returns (trusted, message).
    """
    thumbprint = ca_thumbprint(cert)
    if thumbprint is None:
        return False, "Ninaivu's certificate could not be read, so it was not installed."
    added = runner(["certutil.exe", "-user", "-addstore", "Root", str(cert)],
                   capture_output=True, text=True, check=False)
    if added.returncode != 0 or not windows_store_holds("Root", thumbprint, runner):
        return False, ("Windows did not add the certificate to Trusted Root Certification "
                       "Authorities (the confirmation may have been declined).")
    message = ("Installed in Trusted Root Certification Authorities for this Windows account. "
               "Restart the browser so it picks the certificate up.")
    if windows_store_holds("CA", thumbprint, runner):
        message += (" An earlier copy is also in Intermediate Certification Authorities, "
                    "which does nothing for trust; it is harmless, and can be removed in "
                    "certmgr.msc if you prefer.")
    return True, message
