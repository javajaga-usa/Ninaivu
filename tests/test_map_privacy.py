"""The map, and the one request Ninaivu used to make without being asked.

Everything else in this project is arithmetic on files the household owns.
The map was the exception: opening it fetched tiles from OpenStreetMap, one
request per square of the world on screen, so somebody else's log learned
where a family spends its summers and roughly when it was reminiscing. Nobody
turned that on and nothing said it was happening.

So the map is now drawn from an outline that ships in this repository, and
the tiles are a setting. These tests are mostly about the *off* case, because
that is the promise: with `map_tiles` off there must be no way for the page to
reach a tile server, not in the script, not in the policy that would permit
it, and not through a link a mis-click could follow.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ninaivu" / "static" / "js" / "app.js").read_text(encoding="utf-8")
VIEWER_JS = (ROOT / "ninaivu" / "static" / "js" / "viewer.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ninaivu" / "templates" / "index.html").read_text(encoding="utf-8")
WORLD = ROOT / "ninaivu" / "static" / "data" / "world.geojson"


def csp(client) -> str:
    return client.get("/").headers.get("Content-Security-Policy", "")


# ---------------------------------------------------------------------------
# The outline that ships with Ninaivu
# ---------------------------------------------------------------------------

def test_the_world_is_in_the_repository():
    assert WORLD.is_file(), "the map has nothing to draw without this"


def test_the_outline_is_a_map_of_the_whole_world():
    doc = json.loads(WORLD.read_text(encoding="utf-8"))
    assert doc["type"] == "FeatureCollection"
    features = doc["features"]
    assert len(features) > 100, "a world with a hundred countries in it is a bug"

    lons, lats = [], []
    for feature in features:
        geometry = feature["geometry"]
        polygons = (geometry["coordinates"] if geometry["type"] == "MultiPolygon"
                    else [geometry["coordinates"]])
        for polygon in polygons:
            for ring in polygon:
                assert len(ring) >= 4 and ring[0] == ring[-1], "a ring must close"
                for lon, lat in ring:
                    lons.append(lon)
                    lats.append(lat)

    # Every continent, not one hemisphere: the point of shipping this is that
    # a family who moved across the world still sees both ends of the move.
    assert min(lons) < -160 and max(lons) > 160
    assert min(lats) < -50 and max(lats) > 70


def test_the_outline_is_small_enough_to_ship():
    """It is fetched over the household's own network, but on a phone.

    Well under a megabyte is the bar; past that the honest answer would be to
    generalise the coastlines further rather than to make the map slow.
    """
    assert WORLD.stat().st_size < 400 * 1024


def test_the_outline_says_where_it_came_from():
    doc = json.loads(WORLD.read_text(encoding="utf-8"))
    assert "Natural Earth" in doc.get("license", "")


# ---------------------------------------------------------------------------
# With tiles off, which is the default
# ---------------------------------------------------------------------------

def test_tiles_are_off_unless_a_household_asks():
    from ninaivu.server.config import Config

    assert Config().map_tiles is False


def test_the_page_does_not_advertise_tiles_by_default(client):
    body = client.get("/").get_data(as_text=True)
    assert "data-map-tiles" not in body


def test_the_policy_does_not_permit_a_tile_server_by_default(client):
    """The CSP is the backstop: even a mistake in the script cannot reach one."""
    policy = csp(client)
    assert policy, "the header is missing entirely"
    assert "openstreetmap" not in policy


def test_the_policy_still_allows_the_things_the_page_needs(client):
    policy = csp(client)
    assert "img-src 'self' data: blob:" in policy
    assert "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


def test_the_tile_url_is_reached_only_through_the_setting():
    """One mention of a tile server in the script, behind one gate."""
    assert APP_JS.count("tile.openstreetmap.org") == 1
    gate = APP_JS.index("function wantsTiles()")
    tiles = APP_JS.index("tile.openstreetmap.org")
    assert gate < tiles, "the check must exist before the request it guards"
    assert "if (wantsTiles())" in APP_JS


def test_the_outline_is_what_is_drawn_when_tiles_are_off():
    assert "/static/data/world.geojson" in APP_JS
    assert "L.geoJSON" in APP_JS


def test_a_photograph_does_not_link_its_coordinates_out_by_default():
    """A mis-click on the Location row used to be enough to publish them."""
    built = VIEWER_JS.index("link.href = `https://www.openstreetmap.org")
    opens = VIEWER_JS.rindex("if (", 0, built)
    condition = VIEWER_JS[opens:VIEWER_JS.index("\n", opens)]
    assert "dataset.mapTiles" in condition, condition
    assert "'Location'" in condition, condition


# ---------------------------------------------------------------------------
# With tiles on, because a household said so
# ---------------------------------------------------------------------------

@pytest.fixture()
def tiled(scanned):
    from ninaivu import create_app

    cfg, _, _ = scanned
    cfg.watch = False
    cfg.map_tiles = True
    application = create_app(cfg)
    application.config["MV_SCANNER"].stop()
    return application.test_client()


def test_asking_for_tiles_permits_them(tiled):
    policy = csp(tiled)
    assert "https://*.tile.openstreetmap.org" in policy
    # Both directives, or the images load and the retina requests do not.
    assert policy.count("tile.openstreetmap.org") >= 4


def test_asking_for_tiles_tells_the_page(tiled):
    assert 'data-map-tiles="1"' in tiled.get("/").get_data(as_text=True)


def test_the_switch_is_on_the_command_line():
    main = (ROOT / "ninaivu" / "__main__.py").read_text(encoding="utf-8")
    assert '"--map-tiles"' in main and "cfg.map_tiles = True" in main


# ---------------------------------------------------------------------------
# That the map is actually on screen
# ---------------------------------------------------------------------------

STYLE = (ROOT / "ninaivu" / "static" / "css" / "style.css").read_text(encoding="utf-8")


def test_the_icon_rule_does_not_size_the_map():
    """`svg { width: 20px }` styles every icon in Ninaivu. It also styled the
    one Leaflet draws the world into.

    Leaflet sizes that element with width and height *attributes*, which lose
    to any stylesheet rule, so the world was clipped to a 20px square parked
    off the corner of the map: 177 country paths in the DOM, correct
    geometry, correct colours, and a blank panel on screen. Tiles never
    showed it because tiles are `<img>`. Shipping the outline is what made
    the icon rule reach the map.
    """
    icons = STYLE.index("svg { width: 20px")
    override = STYLE.index(".leaflet-pane > svg")
    assert icons < override, "the override must come after the rule it undoes"
    rule = STYLE[override:STYLE.index("}", override)]
    assert "width: auto" in rule and "height: auto" in rule, rule


# ---------------------------------------------------------------------------
# What the buttons claim
# ---------------------------------------------------------------------------

def test_no_button_claims_a_shortcut_that_does_nothing():
    """`title="Map view (M)"` said M opened the map. M is masonry.

    A tooltip is a promise about what the keyboard does, and a wrong one is
    worse than none: it teaches a key that silently does something else.
    """
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "ninaivu" / "static" / "js").glob("*.js"))
        # Leaflet is somebody else's library and binds nothing Ninaivu advertises.
        if path.name != "leaflet.js")
    handled = {k.lower() for k in re.findall(
        r"key(?:\.toLowerCase\(\))? === '([A-Za-z0-9])'|case '([A-Za-z0-9])'",
        scripts) for k in k if k}
    assert handled, "no keyboard handling found — this test has gone stale"

    claimed = re.findall(r'title="[^"]*\(([A-Za-z0-9])\)"', INDEX)
    assert claimed, "no shortcuts are advertised — this test has gone stale"
    unhandled = sorted({k for k in claimed if k.lower() not in handled})
    assert not unhandled, f"advertised but not wired: {unhandled}"
