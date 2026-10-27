"""What the UI pass of 27 October 2026 found, so it stays found.

Every screen of the family app and the console was opened on a desktop and
a phone, light and dark, in English and Tamil, and checked for fields
squashed flat, text too faint to read, labels Tamil cannot carry, buttons
too small for a finger and English left on a Tamil page. These are the
fixes, held in place.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_dark_theme import _tokens, contrast

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "ninaivu" / "static" / "css"
JS = ROOT / "ninaivu" / "static" / "js"


def _css(name: str) -> str:
    return (CSS / name).read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


def light() -> dict[str, str]:
    return _tokens(_css("style.css"), ":root")


def test_a_field_in_a_column_keeps_its_height():
    # `flex: 1` is a basis of 0: in the password form of Your profile and the
    # Export dialog the fields folded to the height of their text.
    assert "flex: 1 1 auto" in _rule(_css("style.css"), ".input")


def test_a_white_label_on_the_light_accent_reads():
    tokens = light()
    assert contrast(tokens["--accent-fg"], tokens["--accent"]) >= 4.5
    assert contrast(tokens["--accent"], tokens["--surface"]) >= 4.5


def test_the_faintest_light_text_still_reads():
    tokens = light()
    for surface in ("--surface", "--bg-elev", "--bg"):
        assert contrast(tokens["--text-3"], tokens[surface]) >= 4.5, surface
    assert contrast(tokens["--text-3"], tokens["--surface-2"]) >= 4.2


def test_warning_and_all_well_words_are_deep_enough_to_read():
    admin = _css("admin.css")
    warn = re.search(r"--warn:\s*(#[0-9a-f]{6})", admin).group(1)
    ok = re.search(r"--ok-text:\s*(#[0-9a-f]{6})", admin).group(1)
    assert contrast(warn, "#ffffff") >= 4.5 and contrast(ok, "#ffffff") >= 4.5
    for selector in (".pf-level.consider", ".pf-level.good", ".job-status-state.is-unavailable"):
        assert "var(--gold)" not in _rule(admin, selector)
        assert "#22a45d" not in _rule(admin, selector)


def test_tamil_group_labels_are_solid_and_readable():
    premium = _css("premium.css")
    block = premium[premium.index("/* Tamil labels"):]
    for name in (".side-title", ".bar-menu-head", ".tab-group-label", "th", ".field > span"):
        assert name in block, name
    assert "letter-spacing: 0" in block and "text-transform: none" in block


def test_a_tamil_page_says_its_counts_and_misses_in_tamil():
    app = (JS / "app.js").read_text(encoding="utf-8")
    assert "`${count} selected`" not in app
    assert "i18n.t('{count} selected', { count })" in app
    assert "`Nothing matched" not in app
    assert "kind.textContent = entry.type;" not in app


def test_the_phone_viewer_buttons_keep_their_width():
    css = _css("style.css")
    assert ".viewer-tools > .icon-btn { flex: none; width: 32px; }" in css


def test_a_filter_can_be_removed_with_a_finger():
    rule = _rule(_css("style.css"), ".pill button")
    assert "width: 24px" in rule and "height: 24px" in rule


def test_every_advanced_field_is_named():
    advanced = (JS / "advanced.js").read_text(encoding="utf-8")
    assert advanced.count("setAttribute('aria-label', setting.name)") == 2
