"""The text of each face's JavaScript, for tests that look for something in it.

app.js and admin.js were each one file; since 4.0 their pages are modules of
their own under static/js/pages/, and the helpers they share are in ui.js and
store.js. A test that asks "does the family app do X" reads all of it.
"""
from __future__ import annotations

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "ninaivu" / "static" / "js"
SHARED = ("ui.js", "store.js")


def _read(names) -> str:
    return "\n".join((JS / name).read_text(encoding="utf-8") for name in names)


def family_app() -> str:
    """app.js, the family app's pages and the shared helpers."""
    pages = sorted(p.relative_to(JS).as_posix() for p in (JS / "pages" / "family").glob("*.js"))
    return _read(["app.js", *pages, *SHARED])


def console() -> str:
    """admin.js, the console's pages and the shared helpers."""
    pages = sorted(p.relative_to(JS).as_posix() for p in (JS / "pages" / "console").glob("*.js"))
    return _read(["admin.js", *pages, *SHARED])
