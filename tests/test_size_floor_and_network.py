"""The 60 KB media floor, the cross-app links, and serving the network.

Three changes that are easy to half-do: a floor applied in one of the two
walkers, a link that renders correct on one tab and "#" on another, and a
default that only takes effect on one of the two entry points.
"""

from pathlib import Path

from PIL import Image

from conftest import ADMIN, login
from ninaivu.server import auth
from ninaivu.server.config import Config


def photo(path: Path, kb: int) -> Path:
    """A JPEG of roughly *kb* kilobytes — noise is the cheapest way to get size."""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    side = 8
    while True:
        img = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
        img.save(path, "JPEG", quality=95)
        if path.stat().st_size >= kb * 1024 or side > 4096:
            return path
        side *= 2


# --- the floor, in the library walker --------------------------------------

def test_the_library_skips_media_under_the_floor(tmp_path):
    from ninaivu.media.scanner import walk_media

    root = tmp_path / "lib"
    photo(root / "real.jpg", 120)
    Image.new("RGB", (16, 16), (200, 30, 30)).save(root / "icon.png")
    (root / "sig.gif").write_bytes(b"GIF89a" + b"\0" * 400)

    cfg = Config()
    cfg.min_media_bytes = 60 * 1024
    found = {rel for rel, _ in walk_media(root, cfg)}
    assert found == {"real.jpg"}, f"the floor let something small through: {found}"


def test_the_floor_can_be_turned_off(tmp_path):
    from ninaivu.media.scanner import walk_media

    root = tmp_path / "lib"
    photo(root / "real.jpg", 120)
    Image.new("RGB", (16, 16), (200, 30, 30)).save(root / "icon.png")

    cfg = Config()
    cfg.min_media_bytes = 0
    assert len({rel for rel, _ in walk_media(root, cfg)}) == 2


def test_the_floor_defaults_to_sixty_kilobytes():
    assert Config().min_media_bytes == 60 * 1024


# --- the floor, in the archive engine --------------------------------------

def test_the_archive_skips_media_under_the_floor(tmp_path):
    from ninaivu.archive import scanner as archive_scanner
    from ninaivu.archive.scanner import ArchiveJob

    source = tmp_path / "card"
    photo(source / "real.jpg", 120)
    Image.new("RGB", (16, 16), (30, 200, 30)).save(source / "icon.png")

    before = archive_scanner.MIN_MEDIA_BYTES
    archive_scanner.MIN_MEDIA_BYTES = 60 * 1024
    try:
        job = ArchiveJob([str(source)], str(tmp_path / "dest"))
        taken = {name for _, name in job._walk()}
    finally:
        archive_scanner.MIN_MEDIA_BYTES = before
    assert taken == {"real.jpg"}, f"the archive would sweep up noise: {taken}"
    assert job.too_small == 1


def test_ninaivu_hands_its_floor_to_the_archive(tmp_path):
    from ninaivu import archive
    from ninaivu.archive import scanner as archive_scanner

    before = archive_scanner.MIN_MEDIA_BYTES
    try:
        archive.configure(tmp_path / "state", 12345)
        assert archive_scanner.MIN_MEDIA_BYTES == 12345
    finally:
        archive_scanner.MIN_MEDIA_BYTES = before


# --- the cross-app links ---------------------------------------------------

def test_status_carries_both_ports_so_each_app_can_link_to_the_other(as_family):
    status = as_family.get("/api/status").get_json()
    assert status["admin_port"], "the home page has nothing to build a console link from"
    assert status["home_port"]


def test_the_console_overview_carries_the_family_port(scanned):
    from ninaivu import build_services, create_admin_app

    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_admin_app(services).test_client(), *ADMIN)
    app = client.get("/api/admin/overview").get_json()["app"]
    assert app["home_port"] and app["admin_port"]


# --- serving the network by default ----------------------------------------

def test_ninaivu_serves_the_network_by_default():
    assert Config().host == "0.0.0.0", (
        "the default must reach phones and tablets without anyone passing a flag")


def test_local_only_puts_it_back_on_this_machine():
    from ninaivu.__main__ import build_parser

    args = build_parser().parse_args(["--local-only"])
    assert args.local_only is True
