"""The pages split out of app.js and admin.js (static/js/pages/): each is
loaded when it is opened, and fetched at a stamped URL like every module."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static"
JS = STATIC / "js"
DYNAMIC = re.compile(r"""\bimport\(\s*['"](\./pages/[^'"]+)['"]\s*\)""")
ENTRIES = {"app.js": "family", "admin.js": "console"}


def _pages(entry: str) -> list[str]:
    return DYNAMIC.findall((JS / entry).read_text(encoding="utf-8"))


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_every_page_import_names_a_module_of_its_own_face(entry):
    for spec in _pages(entry):
        target = (JS / spec).resolve()
        assert target.is_file(), spec
        assert target.parent == (JS / "pages" / ENTRIES[entry]).resolve(), spec


def test_every_page_module_is_loaded_by_its_entry_point():
    for face, entry in (("family", "app.js"), ("console", "admin.js")):
        named = {Path(spec).name for spec in _pages(entry)}
        for module in (JS / "pages" / face).glob("*.js"):
            if module.name == "common.js":
                continue
            assert module.name in named, f"{module.name} is never opened by {entry}"


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_pages_are_in_the_import_map_stamped_and_not_preloaded(app, entry):
    from ninaivu.server.modules import page_modules

    def asset(url: str) -> str:
        return f"{url}?v=stamp"

    with app.test_request_context("/"):
        html = str(page_modules(STATIC, f"/static/js/{entry}", asset))
    importmap = re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1)
    preloaded = re.findall(r'<link rel="modulepreload" href="([^"]+)">', html)
    for spec in _pages(entry):
        url = "/static/js/" + spec.removeprefix("./")
        assert f'"{url}":"{url}?v=stamp"' in importmap, url
        assert f"{url}?v=stamp" not in preloaded, f"{url} is preloaded but only needed when opened"
    # What the pages themselves import is in the map too.
    for module in ("ui.js", "store.js", "api.js", "http.js"):
        assert f'"/static/js/{module}":' in importmap, module


def test_the_shared_modules_are_preloaded_at_start(app):
    from ninaivu.server.modules import page_modules

    with app.test_request_context("/"):
        html = str(page_modules(STATIC, "/static/js/admin.js", lambda u: f"{u}?v=s"))
    preloaded = re.findall(r'<link rel="modulepreload" href="([^"]+)">', html)
    for module in ("ui.js", "store.js", "api.js", "http.js"):
        assert f"/static/js/{module}?v=s" in preloaded, module


#: The family's moments and the things it makes: fetched when first opened.
FEATURES = ("ask-family.js", "family-tree.js", "books.js", "films.js", "our-year.js",
            "life-story.js", "festivals.js", "young-faces.js", "then-and-now.js", "frame.js",
            "capsules.js", "ask-ninaivu.js")


def test_the_family_features_are_not_in_the_gallerys_start(app):
    """Not imported by the gallery at start, so not preloaded: about 200 KB a
    phone no longer fetches before the first photograph. Still in the import
    map, stamped, for when one is opened."""
    from ninaivu.server.modules import page_modules, static_graph

    start = {p.name for p in static_graph(JS / "app.js", JS)}
    assert not start & set(FEATURES), sorted(start & set(FEATURES))
    with app.test_request_context("/"):
        html = str(page_modules(STATIC, "/static/js/app.js", lambda u: f"{u}?v=s"))
    importmap = re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1)
    for name in FEATURES:
        assert f'"/static/js/{name}":"/static/js/{name}?v=s"' in importmap, name
