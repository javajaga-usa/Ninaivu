"""Every GET address answers, for every caller, on an empty or a damaged library.

Asked in November 2026 to look at each condition from more than one side:
the administrator, a family member, a guest and somebody not signed in; a
library with nothing in it yet; and one holding what real drives hold, an
empty file, a JPEG cut short, a video and a HEIC that are noise, a PNG whose
header claims 3.6 billion pixels, a WAV with no sound, a Tamil folder and
file name with punctuation, and a link that points back at its own folder.
The scan has to finish over it and no address may answer with a server
error. The sweep found one: the editor's Auto suggestion fell over on the
enormous PNG (DecompressionBombError is not an OSError).
"""

from __future__ import annotations

import os
import shutil
import struct
import zlib

import pytest
from werkzeug.routing import IntegerConverter

from conftest import ADMIN, FAMILY, GUEST, login

#: Streams and actions that end the server, not answers to ask for.
SKIP = ("/events", "shutdown", "restart", "stream", "/dav")

#: A 5xx that is the honest answer here, not a fault.
EXPECTED = {
    # Reading a phone straight from the shell is Windows' alone.
    "/api/devices/browse": 501,
}


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _damage(library) -> None:
    bad = library / "damaged" / "நினைவு 2024"
    bad.mkdir(parents=True)
    good = next(library.rglob("shot0.jpg")).read_bytes()
    (bad / "zero.jpg").write_bytes(b"")
    (bad / "half.jpg").write_bytes(good[: len(good) // 3])
    (bad / "noise.mp4").write_bytes(os.urandom(5000))
    (bad / "noise.heic").write_bytes(os.urandom(3000))
    (bad / "not really.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(200))
    header = struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0)
    (bad / "enormous.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(b"\0" * 1000)) + _chunk(b"IEND", b""))
    (bad / "தமிழ் படம் #1 & 'x'.jpg").write_bytes(good)
    (bad / "silent.wav").write_bytes(b"RIFF" + os.urandom(100))
    try:
        os.symlink(bad, bad / "loop")
    except OSError:
        pass                                    # Windows without the right


@pytest.fixture(params=["empty", "damaged"])
def state(request, library):
    if request.param == "empty":
        for path in list(library.iterdir()):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    else:
        _damage(library)
    return request.param


def test_no_address_answers_with_a_server_error(state, scanned):
    from ninaivu import create_app
    from ninaivu.server import auth

    cfg, conn, _ = scanned
    app = create_app(cfg)
    app.config["MV_SCANNER"].stop(join=True)
    try:
        admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
        auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                         role=auth.ROLE_FAMILY, created_by=admin.id)
        auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                         role=auth.ROLE_GUEST, created_by=admin.id)
        if state == "damaged":
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM assets WHERE rel_path LIKE 'damaged/%'")]
            assert len(ids) >= 6, "the scan should have taken most of the damaged files in"
        else:
            assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
            ids = [1]
        callers = {"nobody": app.test_client(), "admin": login(app.test_client(), *ADMIN),
                   "family": login(app.test_client(), *FAMILY),
                   "guest": login(app.test_client(), *GUEST)}
        urls = app.url_map.bind("localhost")
        failures, asked = [], 0
        for rule in app.url_map.iter_rules():
            if "GET" not in rule.methods or any(s in rule.rule for s in SKIP):
                continue
            if state == "damaged" and not rule._converters:
                continue                         # the empty library asks those
            for item in ids:
                values = {name: item if isinstance(conv, IntegerConverter) else "x"
                          for name, conv in rule._converters.items()}
                path = urls.build(rule.endpoint, values, method="GET")
                for who, client in callers.items():
                    asked += 1
                    code = client.get(path).status_code
                    if code >= 500 and EXPECTED.get(rule.rule) != code:
                        failures.append(f"{who} {path} -> {code}")
        assert asked > 400
        assert not failures, "\n".join(failures)
    finally:
        services = app.config.get("MV_SERVICES")
        if services is not None:
            services.stop(timeout=5.0)
