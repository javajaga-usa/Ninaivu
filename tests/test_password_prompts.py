"""The password dialogs share one field (tests/password_prompt_ui.mjs drives them).

These are the cheap checks that keep it that way: no password or PIN is asked
for with the browser's prompt(), every dialog's wrong-password note is read out,
and Enter never presses the Show button that now sits inside the box.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "ninaivu" / "static" / "js"
TEMPLATES = ROOT / "ninaivu" / "templates"


def test_no_password_or_pin_is_asked_for_with_the_browser_prompt():
    for script in sorted(JS.glob("*.js")):
        calls = re.findall(r"(?<![.\w])prompt\(", script.read_text(encoding="utf-8"))
        assert not calls, f"{script.name} still calls prompt()"


def test_every_password_dialog_reads_out_its_error():
    for page, ids in {
        "index.html": ["delete-error"],
        "admin.html": ["purge-error", "lf-delete-error", "lf-replace-error"],
    }.items():
        source = (TEMPLATES / page).read_text(encoding="utf-8")
        for name in ids:
            assert re.search(rf'id="{name}" role="alert"', source), f"{page}: #{name}"


def test_the_dialogs_get_show_and_hide():
    admin = (JS / "admin.js").read_text(encoding="utf-8")
    app = (JS / "app.js").read_text(encoding="utf-8")
    assert "'.modal .field input[type=password]'" in admin and "upgradeSecret(input)" in admin
    assert "upgradeSecret($('#delete-password'))" in app
    assert app.count("upgradeSecret(") >= 2          # the Turn dialog builds its own
    assert "secretBox(field)" in (JS / "lock.js").read_text(encoding="utf-8")


def test_enter_never_presses_show_or_hide():
    source = (JS / "enter-key.js").read_text(encoding="utf-8")
    rule = re.search(r"NOT_THE_ACTION = /\^\(([^)]*)\)", source).group(1).split("|")
    assert "show" in rule and "hide" in rule


def test_the_box_is_sized_the_same_in_safari():
    """Safari sizes a text box from its appearance and line height and draws
    password dots larger: those are set, the text is 16px so an iPhone does
    not zoom in, and the dots get no extra spacing."""
    css = (ROOT / "ninaivu" / "static" / "css" / "premium.css").read_text(encoding="utf-8")
    block = css[css.index("Password dialogs:"):]
    rule = re.search(r"\.modal-card \.field \.input, \.lock-card \.lock-secret \{([^}]*)\}", block).group(1)
    for needed in ("-webkit-appearance: none", "appearance: none", "box-sizing: border-box",
                   "height: 44px", "font-size: 16px", "line-height: 20px", "letter-spacing: normal"):
        assert needed in rule, needed
    assert "letter-spacing: .18em" not in block
