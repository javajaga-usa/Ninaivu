"""Through Tailscale from somewhere else, a round trip is a fifth of a second
and everything goes up the house's internet connection. Measured in Chromium
through a link of 200 ms and 5 Mbit/s: opening the gallery again took 2.5 s
before (a module at a time, each asked about again, then the start-up
questions one after the other) and 1.3 s after."""

import base64
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "ninaivu/static/js"


def _importmap(html: str) -> str:
    return re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1)


@pytest.mark.parametrize("page", ["index.html", "admin.html", "share.html", "ask.html"])
def test_every_page_names_its_modules_before_its_entry_point(page):
    html = (ROOT / "ninaivu/templates" / page).read_text(encoding="utf-8")
    head = html[:html.index("</head>")]
    assert "{{ modules('/static/js/" in head
    assert html.index("{{ modules(") < html.index('<script type="module"')


def test_the_modules_are_fetched_stamped_and_kept(client):
    response = client.get("/")
    html = response.get_data(as_text=True)
    text = _importmap(html)
    stamp = re.search(r'"/static/js/grid\.js":"/static/js/grid\.js\?v=([0-9a-f]+)"', text).group(1)
    # Everything the gallery needs at start is asked for at once.
    for module in ("grid.js", "grid-base.js", "preload-policy.js", "studio/colour.mjs"):
        assert re.search(rf'<link rel="modulepreload" href="/static/js/{re.escape(module)}\?v=', html), module
    # The one inline script the policy allows, by its exact hash.
    digest = base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
    policy = response.headers["Content-Security-Policy"]
    assert f"script-src 'self' 'sha256-{digest}';" in policy
    assert "'unsafe-inline'" not in policy.split("script-src", 1)[1].split(";", 1)[0]
    # A stamped module is kept for a year; a bare one is asked about.
    kept = client.get(f"/static/js/grid.js?v={stamp}")
    assert "immutable" in kept.headers["Cache-Control"]
    assert client.get("/static/js/grid.js").headers["Cache-Control"] == "no-cache"


def test_a_response_without_a_map_allows_no_inline_script(client):
    policy = client.get("/healthz").headers.get("Content-Security-Policy", "")
    assert "sha256-" not in policy


def test_the_graph_follows_imports_into_subfolders():
    from ninaivu.server.modules import static_graph

    names = {p.relative_to(JS).as_posix() for p in static_graph(JS / "app.js", JS)}
    assert {"app.js", "grid.js", "grid-base.js", "preload-policy.js", "studio/colour.mjs"} <= names
    # Loaded on demand only: not preloaded.
    assert not any(n.startswith("ai-playground/") for n in names)


def test_start_up_asks_for_status_beside_the_lock():
    app = (JS / "app.js").read_text(encoding="utf-8")
    start = app[app.index("async function start(user)"):app.index("function renderIdentity()")]
    assert start.index("const statusAsked = api.status()") < start.index("await screenLock?.start(state.user)")
    assert "await refreshStatus(statusAsked)" in start


def test_more_thumbnails_at_once_over_one_connection():
    policy = (JS / "preload-policy.js").read_text(encoding="utf-8")
    assert "return Math.min(8, base + 4);" in policy


def test_away_from_home_a_large_photograph_opens_as_the_viewing_copy(as_family, monkeypatch):
    client = as_family
    from ninaivu.api import api

    monkeypatch.setattr(api, "AWAY_VIEW_COPY_OVER", 0)
    home = client.get("/api/assets?limit=50").get_json()["items"]
    pictures = [i for i in home if i["kind"] == "picture" and i["ext"].lower() in ("jpg", "jpeg")]
    assert pictures and all(i["view"] == f"/api/file/{i['id']}" for i in pictures)

    # Tailscale Serve: from this computer (signed in: open browsing is for home), with the visitor's address forwarded.
    away = {"X-Forwarded-For": "100.101.102.103"}
    items = client.get("/api/assets?limit=50", headers=away).get_json()["items"]
    jpeg = next(i for i in items if i["id"] == pictures[0]["id"])
    assert jpeg["view"] == f"/api/preview/{jpeg['id']}?fit=1"
    assert jpeg["src"] == f"/api/file/{jpeg['id']}"          # the original is still there

    copy = client.get(jpeg["view"], headers=away)
    assert copy.status_code == 200 and copy.mimetype == "image/jpeg"
    from io import BytesIO
    from PIL import Image
    assert not Image.open(BytesIO(copy.data)).getexif()   # no camera, no place


def test_small_photographs_and_home_keep_the_file_itself(as_family):
    client = as_family
    away = {"X-Forwarded-For": "100.101.102.103"}
    items = client.get("/api/assets?limit=50", headers=away).get_json()["items"]
    # The fixtures are a few kilobytes: well under the threshold.
    assert all(not i["view"].endswith("?fit=1") for i in items)
