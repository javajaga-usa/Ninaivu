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


class Said(str):
    """A message with a name or a number in it, as both things at once.

    As a string it is the English filled in (``filled``), so a log, a test or
    an older console reads it as before. It also keeps the template and the
    values, which the JSON error handler sends as ``said`` and ``params``: the
    page then translates the template and fills it in itself, the way
    ``i18n.t(key, vars)`` does — "{name} is too large." in Tamil puts the name
    where Tamil puts it.

    The template is listed in ``ninaivu/messages.py`` with every other
    sentence the server sends, which is how the locale guard knows it.
    """

    template: str
    params: dict

    def __new__(cls, template: str, **params):
        made = super().__new__(cls, filled(template, params))
        made.template = template
        made.params = {name: str(value) for name, value in params.items()}
        return made


def refusal(text: str, **params) -> dict:
    """The body of an error answer: ``{"error": …}``, and, for a message with
    values in it, the template and the values for the page to translate."""
    if not params:
        return {"error": text}
    message = Said(text, **params)
    return {"error": str(message), "said": message.template, "params": message.params}


def body_for(message) -> dict:
    """``{"error": …}`` for *message*, with ``said`` and ``params`` when it is
    a :class:`Said` — for the handlers that turn an ``abort`` into JSON."""
    if isinstance(message, Said):
        return {"error": str(message), "said": message.template, "params": message.params}
    return {"error": str(message)}
