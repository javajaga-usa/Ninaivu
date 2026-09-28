"""The offline shell, and the much more interesting question of its scope.

A service worker on a shared family tablet is a small privacy decision wearing
a performance costume. Whatever it caches sits on that device until something
clears it, readable by whoever picks the tablet up next. So most of these
tests are not about offline behaviour at all — they are about what the worker
is forbidden from storing.
"""

import re
from pathlib import Path

import pytest

from conftest import ADMIN, login

SW = Path(__file__).resolve().parent.parent / "ninaivu" / "static" / "sw.js"


@pytest.fixture()
def source() -> str:
    return SW.read_text(encoding="utf-8")


# --- it is served where it can actually work -------------------------------

def test_the_worker_is_served_from_the_root(client):
    """A worker can only control paths at or below its own URL. Served from
    /static/ it could never see /api/thumb/, which is half the point."""
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["Content-Type"]


def test_the_worker_itself_is_never_cached(client):
    """A stale worker is the one file a reload cannot fix."""
    response = client.get("/sw.js")
    assert "no-cache" in response.headers.get("Cache-Control", "")


def test_closed_library_can_install_worker_before_sign_in(app):
    app.config["MV_CONFIG"].open_browsing = False
    client = app.test_client()
    assert client.get("/sw.js").status_code == 200
    assert client.get("/api/assets").status_code == 401


def test_the_console_has_no_worker(scanned):
    """Management is not useful offline, and cached machinery belongs least on
    the machine that is meant to be the trusted one."""
    from ninaivu.server import auth
    from ninaivu import build_services, create_admin_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    console = login(create_admin_app(services).test_client(), *ADMIN)
    assert console.get("/sw.js").status_code == 404


# --- what it is allowed to keep --------------------------------------------

def test_it_caches_only_the_shell_and_revalidates_media(source):
    cached = set(re.findall(r"url\.pathname\.startsWith\('([^']+)'\)", source))
    assert cached == {"/static/", "/api/"}, (
        f"the worker decides to cache something new: {cached}")
    assert "fetch(request, { cache: 'no-store' })" in source
    assert "cacheFirst(request, THUMB_CACHE" not in source


@pytest.mark.parametrize("path", [
    "/api/file/",       # originals — large, private, and already downloadable
    "/api/download/",
    "/api/proxy/",
    "/api/segments",    # listings differ per viewer
    "/api/assets",
    "/api/me",
    "/api/auth/",
    "/api/faces/",
])
def test_it_never_caches_anything_per_viewer_or_original(source, path):
    """Caching a listing would hand the next person to pick up the tablet the
    last person's gallery."""
    assert f"'{path}'" not in source, (
        f"{path} appears in the worker; it must go to the network every time")


def test_range_requests_are_left_to_the_network(source):
    """Seeking a video is a Range request. A cache answering one returns the
    whole file for a partial request and playback breaks."""
    assert "headers.has('range')" in source


def test_only_plain_successful_responses_are_stored(source):
    """An opaque or error response cached here would be served back later as
    though it were the real thing — including a 403 for a photograph this
    viewer is not allowed to see."""
    assert "response.status === 200" in source
    assert "response.type === 'basic'" in source


def test_the_thumbnail_cache_is_bounded(source):
    match = re.search(r"MAX_THUMBS\s*=\s*(\d+)", source)
    assert match, "the thumbnail cache has no ceiling"
    assert 0 < int(match.group(1)) <= 5000


def test_signing_out_clears_everything(source):
    """A shared tablet passes between people. What one person cached must not
    outlive their session."""
    assert "ninaivu:forget" in source
    app_js = (SW.parent / "js" / "app.js").read_text(encoding="utf-8")
    assert "forgetCachedShell" in app_js
    assert app_js.count("forgetCachedShell()") >= 2, (
        "clear the cache on sign-out and on sign-in, not just one of them")


def test_old_caches_are_dropped_when_the_version_changes(source):
    assert "CACHE_VERSION" in source
    assert "caches.delete" in source


def test_nothing_is_prefetched_on_install(source):
    """Pre-caching a list of files downloads things this install may never
    open, and goes stale the moment the list is edited."""
    install = source[source.index("'install'"):source.index("'activate'")]
    assert "addAll" not in install
    assert "cache.put" not in install


def test_navigation_cache_scope():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for service worker behavioral tests")
    subprocess.run([node, str(Path(__file__).with_name("service_worker_scope.mjs"))],
                   check=True, timeout=30)
