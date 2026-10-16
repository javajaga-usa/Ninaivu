"""Audit of 16 October 2026, before the portable release: the fixes that can be
checked without a browser. The family app's sidebar is checked in
tests/sidebar_first_login_ui.mjs."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "ninaivu" / "static" / "js"


# --- an idle connection closed on purpose is not an error -------------------------------

def test_an_idle_connection_closed_by_its_timeout_is_logged_as_debug(monkeypatch):
    """The HTTPS listener closes a connection idle for IDLE_SECONDS. Python's
    handler logs that as an error, so a phone on Tailscale filled the log and
    the console's recent problems with "Request timed out" every few minutes."""
    from werkzeug import serving
    from ninaivu.server.http import IdleClosingHandler

    said = []
    monkeypatch.setattr(serving, "_log", lambda kind, message, *args: said.append(kind))
    handler = IdleClosingHandler.__new__(IdleClosingHandler)
    handler.address_string = lambda: "100.64.0.1"
    handler.log_date_time_string = lambda: "now"
    handler.log_error("Request timed out: %r", TimeoutError("read"))
    handler.log_error("Something else went wrong: %s", "x")
    assert said == ["debug", "error"]


# --- temporary passwords --------------------------------------------------------------

def test_a_temporary_password_is_drawn_from_a_cryptographic_generator():
    """Two words of twelve and a two-digit number from Math.random was 12,816
    passwords in all."""
    admin = (JS / "admin.js").read_text(encoding="utf-8")
    body = admin[admin.index("function randomPassword()"):]
    body = body[:body.index("\n}\n")]
    assert "Math.random" not in body
    assert "crypto.getRandomValues" in body
    words = re.findall(r"'([a-z]+)'", body)
    assert len(set(words)) >= 64
    assert body.count("pick(") >= 3, "three words"


def test_the_unused_people_panel_is_gone():
    """accounts.js carried a whole people-management panel nothing used, with
    a second, weaker temporary-password generator of its own."""
    accounts = (JS / "accounts.js").read_text(encoding="utf-8")
    assert "class PeoplePanel" not in accounts
    assert "Math.random" not in accounts


# --- the family app speaks the reader's language ----------------------------------------

def test_the_family_app_toasts_and_titles_are_translated():
    app = (JS / "app.js").read_text(encoding="utf-8")
    english = [
        r"toast\(`\$\{scan\.added", r"`Updated \$\{ids\.length\} item", r"`Share Album: ",
        r"`Can’t reach Ninaivu at \$\{", r"`Now visible to \$\{", r"`Remove \$\{label\} filter`",
        r"confirm\(`Delete album", r"`Add \$\{ids\.length\} \$\{", r"'Best';", r"year\$\{diff > 1",
        r"`Uploaded \$\{count\} file",
    ]
    for pattern in english:
        assert not re.search(pattern, app), pattern
    assert "Intl.RelativeTimeFormat" in app, "On This Day says how long ago in the app's language"
