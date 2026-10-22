"""The app's icon and pictures: one drawing, used everywhere (tools/generate_icons.py)."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static"
TEMPLATES = ROOT / "ninaivu" / "templates"
OLD_HOUSE = "M6 15.5 16 7l10 8.5"          # the outline the pages used to draw


def test_each_face_has_its_mark():
    for folder in (STATIC / "icons", STATIC / "icons" / "admin"):
        svg = (folder / "mark.svg").read_text(encoding="utf-8")
        assert svg.startswith("<svg") and 'viewBox="0 0 1024 1024"' in svg
        for size in (180, 192, 512):
            assert Image.open(folder / f"icon-{size}.png").size == (size, size)
        maskable = Image.open(folder / "icon-maskable-512.png").convert("RGBA")
        assert maskable.getpixel((0, 0))[3] == 255, "a maskable icon bleeds to its edges"


def test_pages_show_the_mark_not_the_old_outline():
    for name, mark in (("index.html", "/static/icons/mark.svg"), ("admin.html", "/static/icons/admin/mark.svg"),
                       ("share.html", "/static/icons/mark.svg"), ("ask.html", "/static/icons/mark.svg")):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert OLD_HOUSE not in html, name
        assert f'<link rel="icon" type="image/svg+xml" href="{{{{ asset(\'{mark}\') }}}}">' in html, name
    accounts = (STATIC / "js" / "accounts.js").read_text(encoding="utf-8")
    assert "/static/icons/admin/mark.svg" in accounts and "/static/icons/mark.svg" in accounts


def test_every_start_up_screen_named_is_there_at_its_size():
    for name, folder in (("index.html", STATIC / "splash"), ("admin.html", STATIC / "splash" / "admin")):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        named = set(re.findall(r"splash-(\d+)x(\d+)\.png", html))
        assert len(named) == 20, name
        for w, h in named:
            assert Image.open(folder / f"splash-{w}x{h}.png").size == (int(w), int(h))


def test_mac_icon_keeps_to_apples_grid():
    im = Image.open(ROOT / "installers" / "macos" / "ninaivu.iconset" / "icon_512x512@2x.png").convert("RGBA")
    assert im.size == (1024, 1024)
    assert im.getpixel((40, 512))[3] == 0, "the margin round the tile is clear"
    assert im.getpixel((512, 140))[3] == 255


def test_empty_page_picture_has_a_badge_for_each_reason():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    app = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    for scene in ("search", "folder", "guest", "filter"):
        assert f"ea-badge-{scene}" in html
        assert f'.empty[data-scene="{scene}"] .ea-badge-{scene}' in css
    for scene in ("search", "folder", "guest"):
        assert f"scene: '{scene}'" in app or f"'{scene}' :" in app
    # Drawn in the theme's colours, so it follows light and dark.
    art = html[html.index('class="empty-art empty-art-default"'):html.index('class="empty-art empty-art-offline"')]
    assert "#" not in re.sub(r"url\(#[\w-]+\)|id=\"[\w-]+\"", "", art).replace("<!--", "")
