"""The console's markup and its script have to agree.

Templates used to be compiled once and cached while the CSS and JavaScript
beside them were served with no-cache. After editing a page the browser
therefore ran the *new* script against the *old* markup until somebody
restarted, and a handler bound to an element that did not exist yet threw and
abandoned every control wired after it in the same function. Adding a sign-out
button that way silently killed the storage-integrity button forty lines
below it, with no error anywhere a household would look.

Two things stop that recurring: templates now reload like everything else, and
the bindings no longer depend on each other. These tests hold both.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "ninaivu" / "templates"
SCRIPTS = Path(__file__).resolve().parent.parent / "ninaivu" / "static" / "js"


def test_templates_reload_like_the_files_beside_them(scanned):
    """A template edit must not need a restart *while developing*.

    This used to be unconditional, and the reason was sound: a cached script
    running against freshly edited markup binds a handler to an element that
    does not exist and takes every control after it down. But the cure was a
    debug posture shipped to every household — a stat per render and no asset
    caching at all, for ever.

    The cure now is the version stamp below. The script's URL changes when the
    script does, so markup and script can never be a version apart, and the
    household gets caching back.
    """
    from ninaivu import build_services, create_admin_app, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    cfg.debug = True
    services = build_services(cfg)
    services.scanner.stop()
    for app in (create_admin_app(services), create_home_app(services)):
        assert app.jinja_env.auto_reload is True


def test_the_household_gets_its_caching_back(scanned):
    """…and out of debug, nothing is re-fetched on every single page view."""
    from ninaivu import build_services, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    cfg.debug = False
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    assert app.jinja_env.auto_reload is False
    assert app.config["SEND_FILE_MAX_AGE_DEFAULT"] > 0


def test_a_script_edit_changes_its_url(scanned):
    """What makes the caching above safe: the stamp is the file's own content."""
    from ninaivu import build_services, create_home_app

    cfg, _, _ = scanned
    cfg.watch = False
    cfg.debug = False
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    page = app.test_client().get("/").data.decode("utf-8")
    assert "/static/js/app.js?v=" in page

    script = Path(app.static_folder) / "js" / "app.js"
    before = script.read_text(encoding="utf-8")
    try:
        script.write_text(before + "\n// edited\n", encoding="utf-8")
        app.config["MV_ASSET_V"].clear()
        after = app.test_client().get("/").data.decode("utf-8")
    finally:
        script.write_text(before, encoding="utf-8")
    assert after != page, "editing a script must change the URL it is served at"


def test_no_console_handler_can_abandon_the_ones_after_it():
    """Every id-based binding in the console goes through the always-returns-
    an-element helper, so a missing element skips one control instead of
    stopping the wiring."""
    source = (SCRIPTS / "admin.js").read_text(encoding="utf-8")
    body = source[source.index("function wireChrome() {"):source.index("\nfunction showTab(")]

    # Strip comments first: this very file quotes the broken pattern to
    # explain it, and a lookbehind is needed anyway because "$$('#x')" ends
    # with "$('#x')".
    code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    fragile = re.findall(
        r"(?<![\$\w])\$\('#[a-z0-9-]+'\)\.(?:onclick|onchange|addEventListener)", code)
    assert not fragile, (
        "these bind straight to $() and throw when the element is missing, "
        f"taking the rest of wireChrome with them: {fragile}")


def test_the_storage_integrity_button_exists_and_is_wired():
    """The button the mismatch actually broke."""
    markup = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    script = (SCRIPTS / "admin.js").read_text(encoding="utf-8")

    assert 'id="scrubber-start-btn"' in markup
    assert "#scrubber-start-btn" in script
    assert "wireScrubber()" in script


@pytest.mark.parametrize("element_id", [
    "signout-btn", "scrubber-start-btn", "scrub-total", "scrub-verified",
    "scrub-baseline", "scrub-changed", "scrub-corrupt", "scrub-missing",
    "scrub-unreadable", "scrub-corrupt-card", "scrub-missing-card",
    "scrub-unreadable-card", "faces-review-close", "profile-btn",
])
def test_every_id_the_console_script_reaches_for_is_in_the_page(element_id):
    """Catches the mismatch at build time rather than in somebody's browser."""
    markup = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    script = (SCRIPTS / "admin.js").read_text(encoding="utf-8")
    if f"#{element_id}" not in script:
        pytest.skip(f"{element_id} is not referenced by admin.js")
    assert f'id="{element_id}"' in markup, (
        f"admin.js reaches for #{element_id} but admin.html has no such element")


@pytest.mark.parametrize("element_id", [
    "kiosk-home", "sel-delete", "gate", "profile-sheet",
])
def test_every_id_the_family_script_reaches_for_is_in_the_page(element_id):
    markup = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    script = (SCRIPTS / "app.js").read_text(encoding="utf-8")
    if f"#{element_id}" not in script:
        pytest.skip(f"{element_id} is not referenced by app.js")
    assert f'id="{element_id}"' in markup


# ---------------------------------------------------------------------------
# The shortcut list
# ---------------------------------------------------------------------------
#
# The help modal is documentation that ships inside the product, so it drifts
# the way documentation does. It offered U for upload and M for a map; neither
# key was ever bound to anything, and M actually switches to the masonry
# layout. These tests check the list against the key handlers themselves.

def _help_keys() -> set[str]:
    markup = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    start = markup.index('class="shortcut-group"')
    end = markup.index("</div>", start)
    listed = set(re.findall(r"<dt>([A-Za-z0-9])</dt>", markup[start:end]))
    return {key.lower() for key in listed}


def _bound_keys() -> set[str]:
    app = (SCRIPTS / "app.js").read_text(encoding="utf-8")
    viewer = (SCRIPTS / "viewer.js").read_text(encoding="utf-8")
    grid = set(re.findall(r"key === '([a-z0-9])'", app))
    inside = set(re.findall(r"case '([a-z0-9])':", viewer))
    return grid | inside


def test_every_shortcut_the_help_lists_is_actually_bound():
    listed, bound = _help_keys(), _bound_keys()
    unbound = sorted(listed - bound)
    assert not unbound, (
        f"the help modal offers {unbound}, which no key handler listens for")


def test_the_help_does_not_claim_a_key_means_something_it_does_not():
    """M is the masonry layout, not a map. The map has a button."""
    markup = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    start = markup.index('class="shortcut-group"')
    end = markup.index("</div>", start)
    section = markup[start:end]
    assert "Map Explorer" not in section
    assert "Upload media" not in section


# ---------------------------------------------------------------------------
# Selecting with the keyboard
# ---------------------------------------------------------------------------

def test_shift_is_passed_to_the_grid_when_an_arrow_is_pressed():
    """Without this the range logic below is unreachable from the keyboard."""
    app = (SCRIPTS / "app.js").read_text(encoding="utf-8")
    assert "extend: event.shiftKey" in app


def test_the_range_arithmetic_holds():
    """Runs tests/selection_keys.mjs — the grid's range logic, no browser.

    An add-only range looks right while you extend it and only goes wrong on
    the way back, which is the kind of thing a static check cannot see.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([node, str(root / "tests" / "selection_keys.mjs")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


def test_every_page_the_server_points_at_opens_something():
    """A button the server adds ("Open Extras", a job on Activity) names a page.

    Extras moved into Settings and the Performance page's "Open Extras" went on
    naming the old page, so pressing it did nothing. Every name the server sends
    must be a page in the sidebar or a section the console knows where to find.
    """
    server = Path(__file__).resolve().parent.parent / "ninaivu" / "server"
    named = set()
    for path in server.glob("*.py"):
        named |= set(re.findall(r'(?:"tab": |"page": |page=)"([a-z-]+)"', path.read_text(encoding="utf-8")))
    assert "extras" in named, "the pattern no longer finds what the server names"

    markup = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
    pages = set(re.findall(r'data-tab="([a-z-]+)"', markup))
    script = (SCRIPTS / "admin.js").read_text(encoding="utf-8")
    block = script[script.index("const sectionPages = {"):script.index("\n};", script.index("const sectionPages = {"))]
    sections = dict(re.findall(r"(\w[\w-]*): \{ page: '([a-z-]+)', section: '#[\w-]+' \}", block))
    for page in sections.values():
        assert page in pages
    for anchor in re.findall(r"section: '#([\w-]+)'", block):
        assert f'id="{anchor}"' in markup, f"#{anchor} is not in the console"

    assert not named - pages - set(sections), "the server points at pages the console does not have"
