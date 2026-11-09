"""Core and feature modules (ninaivu/features, 4.0 proposal 1).

Each feature says what it is in one manifest; the loader makes their tables
and puts their routes on in a fixed order, and a fresh index and the route
manifest are byte for byte what they were before the manifests existed.

    python tests/test_features.py --write   # rewrite tests/data/fresh_index_master.txt
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ninaivu import features  # noqa: E402
from ninaivu.server import features as switches  # noqa: E402

MASTER = ROOT / "tests" / "data" / "fresh_index_master.txt"
HEADER = ("# A fresh index's sqlite_master, row by row: type | name | table | root page | sql.\n"
          "# Written by python tests/test_features.py --write; see tests/test_features.py.\n")

#: The order init_db made the features' tables in, in 3.7.
TABLES_IN_37 = ["smart", "ask_family", "family_tree", "books", "handover", "stories", "capsules",
                "screening", "descriptions", "colour_copies", "video_moments", "then_and_now",
                "life_story", "letters", "stacks", "journal"]
#: The order api/api.py imported the features' routes in, in 3.7 (with those
#: that have no tables, which were not features then, in their places).
ROUTES_IN_37 = ["smart", "ask_family", "family_tree", "books", "films", "handover", "stories",
                "screening", "capsules", "frame", "life_story", "letters", "stacks", "colour_copies",
                "video_moments", "then_and_now"]

#: Old module name -> where it lives now.
MOVED = {
    "ninaivu.api.api_books": "ninaivu.features.books.routes",
    "ninaivu.media.books": "ninaivu.features.books.media",
    "ninaivu.storage.books": "ninaivu.features.books.storage",
    "ninaivu.api.api_films": "ninaivu.features.films.routes",
    "ninaivu.media.films": "ninaivu.features.films.media",
    "ninaivu.storage.films": "ninaivu.features.films.storage",
    "ninaivu.media.film_music": "ninaivu.features.films.music",
    "ninaivu.media.storyboard": "ninaivu.features.films.storyboard",
    "ninaivu.api.api_letters": "ninaivu.features.letters.routes",
    "ninaivu.media.letters": "ninaivu.features.letters.media",
    "ninaivu.storage.letters": "ninaivu.features.letters.storage",
    "ninaivu.api.api_life_story": "ninaivu.features.life_story.routes",
    "ninaivu.media.life_story": "ninaivu.features.life_story.media",
    "ninaivu.storage.life_story": "ninaivu.features.life_story.storage",
    "ninaivu.api.api_then_and_now": "ninaivu.features.then_and_now.routes",
    "ninaivu.media.then_and_now": "ninaivu.features.then_and_now.media",
    "ninaivu.storage.then_and_now": "ninaivu.features.then_and_now.storage",
    "ninaivu.api.api_stacks": "ninaivu.features.stacks.routes",
    "ninaivu.storage.stacks": "ninaivu.features.stacks.storage",
    "ninaivu.api.api_capsules": "ninaivu.features.capsules.routes",
    "ninaivu.storage.capsules": "ninaivu.features.capsules.storage",
    "ninaivu.api.api_frame": "ninaivu.features.frame.routes",
    "ninaivu.media.frame": "ninaivu.features.frame.media",
    "ninaivu.api.api_colour": "ninaivu.features.colour_copies.routes",
    "ninaivu.storage.colour_copies": "ninaivu.features.colour_copies.storage",
    "ninaivu.api.api_video_moments": "ninaivu.features.video_moments.routes",
    "ninaivu.media.video_moments": "ninaivu.features.video_moments.media",
    "ninaivu.storage.video_moments": "ninaivu.features.video_moments.storage",
    "ninaivu.api.api_smart": "ninaivu.features.smart.routes",
    "ninaivu.storage.smart": "ninaivu.features.smart.storage",
    "ninaivu.api.api_ask_family": "ninaivu.features.ask_family.routes",
    "ninaivu.storage.ask_family": "ninaivu.features.ask_family.storage",
    "ninaivu.api.api_family_tree": "ninaivu.features.family_tree.routes",
    "ninaivu.storage.family_tree": "ninaivu.features.family_tree.storage",
    "ninaivu.api.api_handover": "ninaivu.features.handover.routes",
    "ninaivu.storage.handover": "ninaivu.features.handover.storage",
    "ninaivu.api.api_stories": "ninaivu.features.stories.routes",
    "ninaivu.storage.stories": "ninaivu.features.stories.storage",
    "ninaivu.api.api_screening": "ninaivu.features.screening.routes",
    "ninaivu.storage.screening": "ninaivu.features.screening.storage",
}


def fresh_master() -> str:
    """A fresh index's sqlite_master, as text."""
    from ninaivu.storage import db
    with tempfile.TemporaryDirectory() as folder:
        conn = db.init_db(os.path.join(folder, "index.db"))
        rows = conn.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY rowid").fetchall()
        db.close_all()
    return HEADER + "".join(" | ".join(str(c) for c in row) + "\n" for row in rows)


# --- The manifest contract ---------------------------------------------------

def test_every_manifest_keeps_the_contract():
    from ninaivu.server.config import Config
    fields = {f.name for f in dataclasses.fields(Config)}
    for feature in features.registry():
        assert feature.problems() == [], feature.name
        assert feature.kind == "feature" and not feature.data_leaves_the_machine
        assert set(feature.settings) <= fields, feature.name
        # The model it needs is the one server/features.py gates it on.
        assert feature.needs == (switches.NEEDS.get(feature.switch) if feature.switch else None)
        assert feature.tier in features.TIERS
        json.dumps(feature.describe())


def test_names_are_unique_and_none_is_core():
    names = features.names()
    assert len(set(names)) == len(names)
    core = {"library", "roles", "faces", "search", "places", "albums", "sharing", "import", "copies"}
    assert not core & set(names)


def test_a_broken_manifest_says_what_is_wrong():
    bad = features.Feature(name="no good", title="", blueprints={"tv": ("api",)},
                           switch="x", tier="huge", data_leaves_the_machine=True)
    problems = " / ".join(bad.problems())
    for words in ("short name", "no title", "'tv'", "routes and blueprints", "switch",
                  "tier", "off this computer"):
        assert words in problems


def test_whether_a_feature_is_on(cfg, monkeypatch):
    books, films, letters = (features.get(n) for n in ("books", "films", "letters"))
    cfg.memory_films = False
    cfg.letters_aloud = False
    assert books.on(cfg) and not films.on(cfg) and not letters.on(cfg)
    cfg.memory_films = True
    cfg.letters_aloud = True
    assert films.on(cfg) and letters.on(cfg)
    colour = features.get("colour_copies")
    cfg.colour_photos = True
    assert colour.on(cfg)
    monkeypatch.setattr(switches, "_why_not", lambda need: "not downloaded")
    switches.forget()
    assert not colour.on(cfg)
    assert {item["name"]: item["on"] for item in features.listing(cfg)}["books"] is True


def test_an_unknown_feature_is_a_key_error():
    with pytest.raises(KeyError):
        features.get("nothing")


# --- The loader's order -------------------------------------------------------

def test_the_loader_order_is_the_old_order():
    assert features.names() == features.ORDER
    with_tables = [f.name for f in features.registry() if f.tables is not None]
    assert with_tables == TABLES_IN_37


def test_tables_are_made_in_order(monkeypatch):
    made = []
    monkeypatch.setattr(features.Tables, "__call__", lambda self, conn: made.append(self.module))
    assert features.create_tables(sqlite3.connect(":memory:")) == TABLES_IN_37
    # ninaivu.features.<name>.storage, or ninaivu.storage.<name> for one not moved.
    assert [m.split(".")[2] if m.startswith("ninaivu.features.") else m.rsplit(".", 1)[-1]
            for m in made] == TABLES_IN_37


def test_routes_are_put_on_in_the_old_order():
    import ninaivu.api  # noqa: F401 - its foot puts the features' routes on
    assert features.routed() == ROUTES_IN_37
    assert set(features.routed()) == {f.name for f in features.registry() if f.routes}


# --- A fresh index is the same file ---------------------------------------------

def test_a_fresh_index_is_what_it_was_before_the_manifests():
    assert fresh_master() == MASTER.read_text(encoding="utf-8"), \
        "a fresh index changed: if that was meant, run python tests/test_features.py --write"


def test_the_index_shape_sees_a_features_tables(monkeypatch):
    from ninaivu.features.books import storage
    from ninaivu.storage import upgrade
    before = upgrade.index_shape()
    monkeypatch.setattr(storage, "SCHEMA", storage.SCHEMA + "\n-- a new column")
    assert upgrade.index_shape() != before


# --- The routes ---------------------------------------------------------------

@pytest.fixture(scope="module")
def apps(tmp_path_factory):
    """The family app and the console, as tools/route_manifest.py makes them."""
    from ninaivu import build_services, create_admin_app, create_home_app
    from ninaivu.server.config import Config
    from ninaivu.storage import db
    logging.disable(logging.WARNING)
    before = os.environ.get("NINAIVU_STATE_DIR")
    os.environ["NINAIVU_STATE_DIR"] = str(tmp_path_factory.mktemp("state"))
    try:
        cfg = Config.load()
        cfg.watch = False
        cfg.ensure_dirs()
        services = build_services(cfg)
        yield {"home": create_home_app(services), "admin": create_admin_app(services)}
    finally:
        db.close_all()
        logging.disable(logging.NOTSET)
        if before is None:
            os.environ.pop("NINAIVU_STATE_DIR", None)
        else:
            os.environ["NINAIVU_STATE_DIR"] = before


def test_the_route_manifest_is_unchanged(apps):
    from ninaivu.server import routes
    text = routes.render(routes.manifest(apps.items()))
    assert text == (ROOT / "tests" / "data" / "route_manifest.txt").read_text(encoding="utf-8")


def test_each_features_routes_are_on_the_blueprints_it_names(apps):
    for feature in features.registry():
        for face, app in apps.items():
            found = features.endpoints(app, feature)
            if not feature.routes:
                assert found == []
                continue
            assert found, (feature.name, face)
            blueprints = {endpoint.split(".", 1)[0] for endpoint in found}
            assert blueprints <= set(feature.blueprints[face]), (feature.name, face, blueprints)


def test_a_switched_off_feature_still_answers_404(app, as_family):
    app.config["MV_CONFIG"].memory_films = False
    assert as_family.get("/api/films").status_code == 404


# --- The old module names -----------------------------------------------------

@pytest.mark.parametrize("old, new", sorted(MOVED.items()))
def test_an_old_name_is_the_very_same_module(old, new):
    import importlib
    assert importlib.import_module(old) is importlib.import_module(new)


def test_patching_the_old_name_patches_the_module(monkeypatch):
    from ninaivu.features.films import media
    from ninaivu.media import films
    monkeypatch.setattr(films, "MAX_COUNT", 7)
    assert media.MAX_COUNT == 7


def test_a_moved_module_still_finds_the_static_folder():
    """A module one folder deeper than it was must look two folders up."""
    from ninaivu.features.books import media
    from ninaivu.features.handover import routes
    static = ROOT / "ninaivu" / "static"
    assert routes.LOCALES == static / "i18n" and routes.LOCALES.is_dir()
    assert media._font_dirs()[0] == static / "fonts"

# --- Extensions in the same shape ---------------------------------------------

def test_an_extension_is_described_by_the_same_manifest():
    from ninaivu import extensions
    module = SimpleNamespace(NAME="cloudy", TITLE="Cloudy", SUMMARY="", register=lambda app, face: None,
                             BLUEPRINTS={"home": ["cloudy"]})
    ext = extensions.Extension(name="cloudy", title="Cloudy", summary="", data_leaves_the_machine=True,
                               destination="somewhere", downloads="", module=module)
    feature = features.from_extension(ext)
    assert feature.kind == "extension" and feature.data_leaves_the_machine
    assert feature.problems() == []
    assert feature.blueprints == {"home": ("cloudy",)}
    assert feature.tier == "full" and "extensions" in feature.settings
    assert not feature.on(SimpleNamespace(extensions=[]))
    assert feature.on(SimpleNamespace(extensions=["cloudy"]))


if __name__ == "__main__":
    if "--write" in sys.argv:
        MASTER.write_text(fresh_master(), encoding="utf-8", newline="\n")
        print(f"wrote {MASTER.relative_to(ROOT)}")
