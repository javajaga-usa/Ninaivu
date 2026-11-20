"""The offline message is wired in both front ends (2026-11-20).

A browser run covers the look; this keeps the parts a refactor could drop
without anybody noticing: the console's request helper reporting a server
that does not answer, the console's listener for it, the family app's
offline class on the page, and the English and Tamil strings for both.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static"


def _read(relative):
    return (STATIC / relative).read_text(encoding="utf-8")


def test_console_request_reports_a_server_that_does_not_answer():
    http = _read("js/http.js")
    assert "export function onUnreachable(handler)" in http
    # The catch around fetch is what turns "no answer" into a notice.
    assert "unreachableHandlers" in http
    assert "handler();" in http


def test_console_shows_its_offline_card_and_reloads_when_back():
    admin = _read("js/admin.js")
    assert "onUnreachable(onConsoleUnreachable)" in admin
    assert "function onConsoleUnreachable()" in admin
    assert "location.reload()" in admin
    assert "'gate-offline'" in admin


def test_family_app_shows_only_the_offline_message():
    app = _read("js/app.js")
    assert "document.body.classList.add('is-offline')" in app
    assert "document.body.classList.remove('is-offline')" in app
    css = _read("css/style.css")
    assert "body.is-offline .sidebar" in css
    assert "body.is-offline .content > :not(#offline-banner):not(#empty)" in css


def test_offline_strings_are_in_english_and_tamil():
    keys = [
        "Ninaivu is offline. Reconnecting automatically…",
        "Ninaivu is offline at {host}. Reconnecting automatically…",
        "Ninaivu is offline",
    ]
    for code in ("en", "ta"):
        table = json.loads(_read(f"i18n/{code}.json"))
        for key in keys:
            assert table.get(key), f"{code} lacks {key!r}"
