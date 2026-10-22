"""The modern pass, read from the files: one scale, glass only where it is
safe, motion only for those who have not asked for less, and a command bar
that offers nothing the person may not see or should not do by accident.

The behaviour itself is driven in a browser by tests/modern_feel_ui.mjs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static"
CSS = STATIC / "css"
JS = STATIC / "js"
PREMIUM = (CSS / "premium.css").read_text(encoding="utf-8")
STYLE = (CSS / "style.css").read_text(encoding="utf-8")
PALETTE = (JS / "palette.js").read_text(encoding="utf-8")
APP = (JS / "app.js").read_text(encoding="utf-8")
MOTION = (JS / "motion.js").read_text(encoding="utf-8")
VIEWER = (JS / "viewer.js").read_text(encoding="utf-8")
ADMIN_HTML = (ROOT / "ninaivu" / "templates" / "admin.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "ninaivu" / "templates" / "index.html").read_text(encoding="utf-8")

MODERN = PREMIUM[PREMIUM.index("The modern pass"):]


def _uncommented(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _rules(text: str) -> list[tuple[list[str], str]]:
    """Every innermost rule, as its selectors and its declarations."""
    return [([name.strip() for name in m.group(1).split(",")], m.group(2))
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _uncommented(text))]


def _block(text: str, opener: str) -> str:
    """The body of the first at-rule that starts with `opener`."""
    start = text.index(opener)
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
    raise AssertionError(f"unclosed {opener}")


def test_the_scale_is_defined_once_in_the_tokens():
    root = STYLE[STYLE.index(":root {"):STYLE.index("color-scheme: light;")]
    for token in ("--sp-1:", "--sp-1h:", "--sp-2:", "--sp-4:", "--r-sm:", "--r-md:",
                  "--r-lg:", "--r-xl:", "--r-pill:", "--dur-1:", "--dur-2:", "--dur-3:"):
        assert token in root, token


def test_menus_popups_cards_and_dialogs_take_their_corners_from_the_scale():
    wanted = {
        ".bar-menu-list": "var(--r-lg)", ".profile-menu-list": "var(--r-lg)",
        ".suggestions": "var(--r-lg)", ".toast": "var(--r-lg)", ".jump-pop": "var(--r-lg)",
        ".library-card": "var(--r-lg)", ".admin .card": "var(--r-lg)",
        ".modal-card": "var(--r-xl)", ".sheet-card": "var(--r-xl)", ".palette-card": "var(--r-xl)",
        ".palette-item": "var(--r-md)", ".bar-menu-list .icon-btn": "var(--r-md)",
    }
    found = {}
    for selectors, body in _rules(MODERN):
        match = re.search(r"border-radius:\s*([^;]+)", body)
        if match:
            for selector in selectors:
                found[selector] = match.group(1).strip()
    for selector, value in wanted.items():
        assert found.get(selector) == value, (selector, found.get(selector))


def test_glass_is_for_menus_and_the_sliding_pane_never_the_top_bars():
    for selectors, body in _rules(MODERN):
        if "backdrop-filter" in body and "none" not in body:
            for selector in selectors:
                assert not re.search(r"topbar|admin-bar|header", selector), selector
    # The pane is glass only where it slides over the photographs.
    supported = _block(MODERN, "@supports")
    narrow = _block(supported, "@media (max-width: 900px)")
    assert ".sidebar" in narrow
    assert ".sidebar" not in supported.replace(narrow, "")
    # And solid again for anyone who asked for less transparency.
    assert ".sidebar" in _block(MODERN, "@media (prefers-reduced-transparency: reduce)")


def test_new_motion_only_plays_when_less_was_not_asked_for():
    gentle = _block(MODERN, "@media (prefers-reduced-motion: no-preference)")
    outside = _uncommented(MODERN.replace(gentle, ""))
    for name in ("float-in", "chrome-in", "skeleton-light"):
        uses = re.findall(rf"animation:[^;]*\b{name}\b", outside)
        assert not uses, (name, uses)
        assert re.search(rf"animation:[^;]*\b{name}\b", gentle), name
    # And the earlier entrances are switched off in one place.
    calm = _block(MODERN, "@media (prefers-reduced-motion: reduce)")
    for selector in (".viewer", ".palette-card", ".modal-card", ".toast", ".bar-menu-list"):
        assert selector in calm, selector


def test_the_flight_is_skipped_for_less_motion_and_needs_no_layer_per_tile():
    assert "export function calm()" in MOTION
    for name in ("growFrom", "shrinkTo"):
        body = MOTION[MOTION.index(f"export async function {name}"):]
        assert "if (calm()" in body[:body.index("\n}\n")], name
    assert "calm()" in VIEWER
    # Only the one picture in flight is promoted; tiles never are.
    tiles = [body for selectors, body in _rules(STYLE + PREMIUM)
             if any(s in (".cell", ".cell img") for s in selectors)]
    assert not any("will-change" in body for body in tiles)


def test_the_skeleton_colours_are_kept_for_each_person_apart():
    assert "skeleton.${state.user?.id" in APP
    assert "rememberSkeleton();" in APP
    # Only the plain gallery's, never a search's.
    body = APP[APP.index("function rememberSkeleton"):]
    assert "anyFilter()" in body[:body.index("\n}\n")]


def test_the_console_command_bar_offers_only_everyday_jobs_that_exist():
    table = PALETTE[PALETTE.index("const CONSOLE_ACTIONS"):]
    table = table[:table.index("];")]
    pairs = re.findall(r"\['([a-z-]+)', '([a-z-]+)'\]", table)
    assert len(pairs) >= 8
    for tab, button in pairs:
        assert f'data-tab="{tab}"' in ADMIN_HTML, tab
        assert f'id="{button}"' in ADMIN_HTML, button
        # Pressed on the person's behalf: never anything that erases, moves,
        # restarts or starts over.
        assert not re.search(r"delete|erase|restart|stop|full|apply|replace|reset|move|mg-",
                             button), button
    # Each is offered only when its page is, and the button is ready.
    assert "!offered(page)" in PALETTE and "node.disabled" in PALETTE


def test_the_family_command_bar_lists_only_what_the_server_gave_this_person():
    body = APP[APP.index("function paletteCommands"):]
    body = body[:body.index("\n}\n")]
    # The sidebar's own lists, which the server filters for the viewer: no
    # fetch of its own that could reach past the rules.
    assert "state.people" in body and "state.occasions" in body
    assert "api." not in body and "fetch(" not in body
    search = APP[APP.index("function paletteSearch"):]
    search = search[:search.index("\n}\n")]
    assert "reload(" in search and "api." not in search


def test_new_keys_are_listed_and_handled():
    for key, label in (("Home / End", "First / last item"), ("[", "Show or hide the side pane"),
                       ("H", "Back to all photos")):
        assert f"<dt>{key}</dt><dd data-i18n=\"{label}\">" in INDEX_HTML, key
    assert "home: 'first', end: 'last'" in APP
    assert "key === '['" in APP and "key === 'h'" in APP


def test_new_words_are_in_tamil():
    tamil = json.loads((STATIC / "i18n" / "ta.json").read_text(encoding="utf-8"))
    english = json.loads((STATIC / "i18n" / "en.json").read_text(encoding="utf-8"))
    for key in ("Admin actions", "Search for “{words}”", "Show or hide the side pane",
                "Back to all photos", "People", "Trips", "Search"):
        assert key in english, key
        assert tamil.get(key) and tamil[key] != key, key
    assert "{words}" in tamil["Search for “{words}”"]


def test_scroll_bars_stay_clear_until_a_box_scrolls():
    bars = PREMIUM[PREMIUM.index("/* Scroll bars:"):]
    bars = _block(bars, "@supports (scrollbar-color: auto)")
    assert "scrollbar-color: transparent transparent" in bars
    assert ":where(.bars-on)" in bars and "html.bars-on" in bars
    # Both apps mark whatever scrolls, without holding the scroll up.
    script = (JS / "scrollbars.js").read_text(encoding="utf-8")
    assert "passive: true" in script and "capture: true" in script
    for app in (APP, (JS / "admin.js").read_text(encoding="utf-8")):
        assert "hideScrollBarsUntilUsed();" in app


def test_resting_tiles_paint_no_shadow_and_no_gradient():
    """A tile is `contain: strict`: a shadow outside it is never seen, and a
    gradient under a photograph is paint every tile pays for in a scroll."""
    for selectors, body in _rules(PREMIUM):
        if ".cell" in selectors:
            assert "box-shadow: none" in body or "box-shadow" not in body, body
            assert "background-image" not in body, body
