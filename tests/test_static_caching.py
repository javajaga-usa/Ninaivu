"""A fingerprinted URL is kept; anything else is still revalidated.

The templates already wrote `?v=<sha>` on every script and stylesheet, so a
changed file has always meant a changed URL. Nothing acted on that: scripts
were sent with `no-cache`, and every page load spent a round trip per file
being told that nothing had changed.
"""



def asset_url(client, path):
    """Ask the app for the URL it would put in the page."""
    from flask import render_template_string

    with client.application.app_context(), client.application.test_request_context():
        return render_template_string("{{ asset('%s') }}" % path)


def test_a_matching_fingerprint_is_kept_forever(client):
    url = asset_url(client, "/static/js/app.js")
    assert "?v=" in url

    response = client.get(url)
    assert response.status_code == 200
    cache = response.headers["Cache-Control"]
    assert "immutable" in cache and "max-age=31536000" in cache


def test_a_wrong_fingerprint_is_not(client):
    """An old tab asking for last month's URL must not be told to keep it."""
    response = client.get("/static/js/app.js?v=beef")
    assert response.status_code == 200
    assert "immutable" not in response.headers["Cache-Control"]
    assert "no-cache" in response.headers["Cache-Control"]


def test_a_bare_script_url_still_revalidates(client):
    response = client.get("/static/js/app.js")
    assert "no-cache" in response.headers["Cache-Control"]


def test_the_page_itself_is_never_kept(client):
    """The HTML is what names the fingerprints, so it has to be fresh."""
    response = client.get("/")
    assert "immutable" not in response.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# The assets no template ever names
# ---------------------------------------------------------------------------
#
# `asset()` can only stamp a path the template writes. Leaflet's stylesheet
# and the world outline are fetched by app.js itself, at bare URLs, so they
# can never carry a matching fingerprint. They were falling through to a
# year-long default and freezing in the browser: the map opened onto a grey
# rectangle drawn by a stylesheet from months ago, and no ordinary reload
# would replace it. Everything unstamped revalidates now, whatever it is.


def test_a_bare_stylesheet_still_revalidates(client):
    response = client.get("/static/css/style.css")
    assert "no-cache" in response.headers["Cache-Control"]
    assert "immutable" not in response.headers["Cache-Control"]


def test_a_wrong_fingerprint_on_a_stylesheet_is_not_kept(client):
    response = client.get("/static/css/style.css?v=beef")
    assert "no-cache" in response.headers["Cache-Control"]
    assert "immutable" not in response.headers["Cache-Control"]


def test_the_map_assets_are_never_frozen(client):
    """The two files the map fetches for itself, at the URLs it uses."""
    for path in ("/static/css/leaflet.css", "/static/data/world.geojson"):
        cache = client.get(path).headers["Cache-Control"]
        assert "no-cache" in cache, f"{path}: {cache}"
        assert "max-age=31536000" not in cache, f"{path}: {cache}"


def test_a_matching_fingerprint_is_still_kept_for_a_stylesheet(client):
    """The fix must not cost the caching it was worth having."""
    url = asset_url(client, "/static/css/style.css")
    cache = client.get(url).headers["Cache-Control"]
    assert "immutable" in cache and "max-age=31536000" in cache
