"""Phone screens: Sudar can be closed, the app reaches the bottom, the
timeline rail does not stay printed over the photographs.

These are layout rules, so they are checked where they live, in the
stylesheets. The browser checks cannot give a page an iPhone's notch.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "ninaivu" / "static"


def _css(name: str) -> str:
    return (STATIC / "css" / name).read_text(encoding="utf-8")


def _block(css: str, opening: str) -> str:
    """The text of the at-rule block that starts with *opening*."""
    start = css.index(opening)
    depth, i = 0, css.index("{", start)
    for j in range(i, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[i:j]
    raise AssertionError(f"unclosed block after {opening!r}")


def test_sudar_fills_a_phone_and_keeps_its_close_button_clear_of_the_notch():
    phone = _block(_css("ai-playground.css"), "@media (max-width: 900px), (max-height: 560px)")
    for dialog in ("#ai-playground", ".ap-portrait-dialog", ".ap-remove-dialog", ".ap-recolor-dialog"):
        assert dialog in phone
    assert "height: var(--app-h, 100dvh)" in phone
    assert re.search(r"\.ap-header[^{]*\{[^}]*padding-top:\s*max\(8px, env\(safe-area-inset-top", phone)
    assert "safe-area-inset-bottom" in phone


def test_photo_studio_header_clears_the_notch_on_a_phone():
    phone = _block(_css("editor.css"), "@media (max-width: 760px)")
    assert re.search(r"\.photo-editor header \{[^}]*env\(safe-area-inset-top", phone)
    assert "height: var(--app-h, 100dvh)" in phone


def test_home_screen_app_on_ios_takes_the_whole_screen():
    css = _css("style.css")
    assert ":root { --app-h: 100dvh; }" in css
    ios = _block(css, "@supports (-webkit-touch-callout: none)")
    assert "--app-h: 100lvh" in ios
    assert "body:not(.admin) { height: var(--app-h); }" in ios
    for layer in (".viewer", ".sheet", ".modal", ".gate"):
        assert layer in ios


def test_timeline_rail_is_not_left_up_by_a_tap():
    css = _css("style.css")
    # :hover sticks after a tap on a phone, so it only raises the rail where
    # there is a real pointer to hover with.
    assert ".content:hover .scrubber, .scrubber.active" not in css
    assert "@media (hover: hover) { .content:hover .scrubber { opacity: 1; } }" in css
    app = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    assert "classList.add('scrolling')" in app


def test_creative_studio_clears_the_notch_on_a_phone():
    css = _css("creative-studio.css")
    phone = css[css.index("@media(max-width:760px)"):]
    assert "height:var(--app-h,100dvh)" in phone
    assert "#creative-studio header{padding-top:max(8px,env(safe-area-inset-top" in phone


def test_photo_details_are_not_covered_by_the_viewer_bar_on_a_phone():
    phone = _block(_css("style.css"), "@media (max-width: 900px)")
    assert ".viewer.info-open .viewer-top { display: none; }" in phone
    assert "env(safe-area-inset-top" in phone[phone.index(".viewer-info {"):]
