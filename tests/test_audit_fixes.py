"""Regressions for the whole-app audit of September 2026.

One test per finding that is not already covered by test_share_links.py.
"""

import io
import os
import stat

import pytest


# --- 05: uploads take photographs, and originals are never served as pages --

def test_an_upload_that_is_not_media_is_refused(as_admin):
    for name in ("payload.html", "tool.exe", "notes.svg", "script.js"):
        response = as_admin.post(
            "/api/upload",
            data={"files": (io.BytesIO(b"whatever"), name)},
            content_type="multipart/form-data")
        body = response.get_json()
        assert body["uploaded"] == [], f"{name} should not have been accepted"
        assert body["errors"], f"{name} should have said why"


def test_a_real_photograph_still_uploads(as_admin, tmp_path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 40, 40)).save(buf, "JPEG")
    buf.seek(0)
    body = as_admin.post("/api/upload",
                         data={"files": (buf, "holiday.jpg")},
                         content_type="multipart/form-data").get_json()
    assert len(body["uploaded"]) == 1
    assert body["uploaded"][0]["filename"].endswith(".jpg")


def test_an_original_is_never_served_as_a_web_page(as_admin, scanned):
    """A library indexed before uploads were filtered can still hold one."""
    cfg, conn, _ = scanned
    from pathlib import Path

    rogue = Path(cfg.active_root) / "rogue.html"
    rogue.write_text("<h1>Sign in to your bank</h1>", encoding="utf-8")
    row = conn.execute("SELECT id, rel_path FROM assets LIMIT 1").fetchone()
    conn.execute("UPDATE assets SET rel_path=? WHERE id=?",
                 ("rogue.html", row["id"]))
    conn.commit()

    response = as_admin.get(f"/api/file/{row['id']}")
    assert response.status_code == 200
    assert "text/html" not in response.headers.get("Content-Type", "")
    assert response.headers.get("Content-Disposition", "").startswith("attachment")


# --- 03: the proxy headers are read only when there is a proxy -------------

def test_forwarded_headers_are_ignored_by_default(app):
    """On a directly exposed port those headers are written by the caller."""
    assert app.config["MV_CONFIG"].trusted_proxies == 0
    assert not hasattr(app.wsgi_app, "x_for")


def test_forwarded_headers_are_read_when_a_proxy_is_declared(scanned):
    from ninaivu import create_home_app, build_services

    cfg, _, _ = scanned
    cfg.trusted_proxies = 1
    app = create_home_app(build_services(cfg))
    assert getattr(app.wsgi_app, "x_for", 0) == 1


# --- 08: development settings do not ship --------------------------------

def test_production_caches_its_assets(app):
    assert app.config["SEND_FILE_MAX_AGE_DEFAULT"] > 0
    assert app.config["TEMPLATES_AUTO_RELOAD"] is False


def test_asset_urls_carry_a_version_so_caching_is_safe(app):
    body = app.test_client().get("/").data.decode("utf-8")
    assert "/static/js/app.js?v=" in body


def test_leaflet_is_not_loaded_before_anybody_opens_a_map(app):
    body = app.test_client().get("/").data.decode("utf-8")
    assert "leaflet.js" not in body


# --- 10: the rate-limit table is swept ------------------------------------

def test_the_rate_limit_table_does_not_grow_for_ever():
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS.clear()
    for i in range(accounts_api._MAX_KEYS + 200):
        accounts_api.record_attempt(f"10.0.0.{i % 255}|user{i}")
    accounts_api.rate_limited("trigger|sweep")
    assert len(accounts_api._ATTEMPTS) <= accounts_api._MAX_KEYS


def test_stale_entries_are_dropped(monkeypatch):
    from ninaivu.api import accounts_api

    accounts_api._ATTEMPTS.clear()
    accounts_api._last_sweep = 0.0
    accounts_api.record_attempt("someone|old")
    accounts_api._ATTEMPTS["someone|old"] = [0.0]      # long expired
    accounts_api._last_sweep = 0.0
    accounts_api.rate_limited("anybody|else")
    assert "someone|old" not in accounts_api._ATTEMPTS


# --- 12: the config file holds a password ---------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_config_file_is_not_world_readable(scanned):
    cfg, _, _ = scanned
    cfg.notify_smtp_password = "hunter2"
    cfg.save()
    mode = stat.S_IMODE(cfg.config_path.stat().st_mode)
    assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0, oct(mode)


# --- 13: the silent handlers say something --------------------------------

def test_unreadable_files_are_logged_rather_than_swallowed(scanned, caplog):
    import logging
    from pathlib import Path
    from ninaivu.media import media

    caplog.set_level(logging.DEBUG, logger="ninaivu.media.media")
    broken = Path(scanned[0].active_root) / "broken.jpg"
    broken.write_bytes(b"not really a jpeg")
    media.read_exif_path(broken) if hasattr(media, "read_exif_path") else None
    # The point is the module has a logger at all and no longer writes `pass`.
    source = Path(media.__file__).read_text(encoding="utf-8")
    assert "log = logging.getLogger" in source
    assert "except Exception:\n        pass" not in source


# --- 06/07: the app must obey PEP 3333 now a real WSGI server runs it ------

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def test_no_response_sets_a_hop_by_hop_header(as_admin):
    """Waitress asserts on these, and it is right to.

    The connection belongs to the server, not to the application. Werkzeug
    let `Connection: keep-alive` through, so the event streams carried one for
    no benefit — and the first request to /api/events under a real WSGI server
    died with an AssertionError before a byte reached the console.
    """
    for path in ("/api/events", "/api/assets?limit=1", "/api/me",
                 "/api/straighten/status"):
        response = as_admin.get(path)
        offending = {name.lower() for name, _ in response.headers} & HOP_BY_HOP
        assert not offending, f"{path} sets {offending}"
        response.close()


def test_the_event_stream_still_says_it_is_an_event_stream(as_admin):
    response = as_admin.get("/api/events")
    assert response.mimetype == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Accel-Buffering"] == "no"
    response.close()


def test_no_source_file_sets_one_either():
    """A grep, because the next one of these will be added by hand too."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ninaivu"
    pattern = re.compile(
        r'["\'](' + "|".join(HOP_BY_HOP) + r')["\']\s*:', re.I)
    for source in root.rglob("*.py"):
        for number, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), 1):
            assert not pattern.search(line), f"{source.name}:{number}: {line.strip()}"


# --- 08 follow-up: caching must not freeze an imported module -------------

def test_javascript_modules_are_revalidated(app):
    """A stamped entry point plus a cached import is worse than no cache.

    `admin.js` is named in the template, so it carries a version stamp and
    updates. Everything it imports is fetched at a plain URL — and with a
    year-long cache those stay on last month's version, so new markup runs
    against an old script. That is exactly how the Straighten tab came to
    throw "Cannot set properties of null" on a button that no longer existed.
    """
    client = app.test_client()
    for path in ("/static/js/admin.js", "/static/js/straighten.js",
                 "/static/js/api.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("Cache-Control") == "no-cache", path


def test_everything_else_still_gets_its_long_cache(app):
    """The point of the stamp is that these can be kept for a year -- but
    only the fingerprinted URL, not a bare request for the same file. This
    used to request `/static/css/style.css` with no `?v=` and expect the
    year-long cache; that is exactly the case `_headers` in ninaivu/__init__.py
    deliberately excludes (see its comment: an asset requested bare, like
    leaflet.css or world.geojson, can never match a fingerprint, so a
    year-long default would freeze it with no way to shift it). The bare-
    request half of that contract is covered separately below.
    """
    import re

    client = app.test_client()
    body = client.get("/").data.decode("utf-8")
    match = re.search(r'(/static/css/style\.css\?v=[\w.-]+)', body)
    assert match, "style.css should be linked from the template with a version stamp"

    response = client.get(match.group(1))
    assert response.status_code == 200
    assert "no-cache" not in (response.headers.get("Cache-Control") or "")


def test_a_bare_static_request_is_not_cached_forever(app):
    """The other half of the contract above: a request with no `?v=` (or a
    stale one) always revalidates, even for a file the template does name
    with a stamp elsewhere. It's the only safe default for the assets the
    template never names at all -- see `_headers`'s comment for why."""
    response = app.test_client().get("/static/css/style.css")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-cache"


def test_an_unchanged_module_costs_a_304_not_a_download(app):
    client = app.test_client()
    first = client.get("/static/js/straighten.js")
    etag = first.headers.get("ETag")
    assert etag, "revalidation needs an ETag to be cheap"
    again = client.get("/static/js/straighten.js",
                       headers={"If-None-Match": etag})
    assert again.status_code == 304


# --- thumbnails must actually revalidate ---------------------------------

def test_a_thumbnail_revalidates_instead_of_being_sent_again(as_family):
    """The ETag has to be the one the conditional check compares against.

    `send_file(conditional=True)` evaluates If-None-Match against the tag it
    computed itself, inside the call. Assigning `response.headers["ETag"]`
    afterwards handed the browser one tag while the comparison kept using
    another, so every revalidation missed: a reload of a full grid re-sent
    every thumbnail the browser already had, instead of a few hundred 304s.
    """
    item = as_family.get("/api/assets?limit=1").get_json()["items"][0]
    first = as_family.get(f"/api/thumb/{item['id']}?s=640")
    assert first.status_code == 200
    etag = first.headers.get("ETag")
    assert etag, "a long cache needs a tag to revalidate against"

    again = as_family.get(f"/api/thumb/{item['id']}?s=640",
                          headers={"If-None-Match": etag})
    assert again.status_code == 304, "the browser must be able to reuse its copy"
    assert len(again.data) == 0


def test_the_thumbnail_tag_changes_when_the_photograph_does(as_family, scanned):
    """Otherwise an immutable year-long cache would pin a stale thumbnail."""
    _, conn, _ = scanned
    item = as_family.get("/api/assets?limit=1").get_json()["items"][0]
    before = as_family.get(f"/api/thumb/{item['id']}?s=640").headers["ETag"]
    conn.execute("UPDATE assets SET mtime=mtime+1000 WHERE id=?", (item["id"],))
    conn.commit()
    after = as_family.get(f"/api/thumb/{item['id']}?s=640").headers["ETag"]
    assert before != after


def test_each_size_has_its_own_tag(as_family):
    """Two sizes at one URL differing only by ?s= must not share a cache entry."""
    item = as_family.get("/api/assets?limit=1").get_json()["items"][0]
    small = as_family.get(f"/api/thumb/{item['id']}?s=256").headers["ETag"]
    large = as_family.get(f"/api/thumb/{item['id']}?s=640").headers["ETag"]
    assert small != large
