"""The console's fixed English, marked where it is written.

The server sends some of what the admin console shows — a setting's group,
a model's purpose, a piece of performance advice — in English. The console
looks each one up in the locale files with ``i18n.t()``, and the locale guard
in ``tests/test_language.py`` has to know those sentences exist, or it calls
their translations unused. ``said`` is how it knows: every literal handed to
it in the package is collected like an ``i18n.t('…')`` in a script.

The same rules as a key in JavaScript: one literal on one line, never a sum
of two strings and never an f-string — the guard reads the source, so a key
built at runtime is a key it cannot see.
"""
from __future__ import annotations


def said(text: str) -> str:
    """Mark fixed English the console shows, so the locale guard finds it; returns it unchanged."""
    return text


def filled(template: str, params: dict) -> str:
    """*template* with each ``{name}`` replaced by ``str(params[name])`` —
    exactly what ``i18n.t(key, vars)`` does in the console, so the English the
    server sends and the English the console would build are the same text.

    A message with a number or a name in it is sent twice: filled in, for
    anything that reads it as it is, and as a ``said`` template plus params,
    for the console to translate before filling in.
    """
    text = template
    for name, value in params.items():
        text = text.replace("{" + name + "}", str(value))
    return text
