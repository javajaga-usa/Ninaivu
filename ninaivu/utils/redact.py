"""What counts as a secret, in one place, and keeping secrets out of the log.

The settings file, the copy of the index kept in Drive, the state bundles
written to another disk and the log all have to agree on what is a secret.
They each had, or lacked, a rule of their own; the Drive copy's rule moved
here so the bundles and the log use the same one (external audit 2026-10-11,
SEC-01 and OBS-01).

The log is written to a file beside the index and the last warnings are shown
on the console's Activity tab. Nothing in Ninaivu logs a password on purpose,
but a message or an exception can carry one by accident: a WebDAV or S3
address with a password in it, a webhook's token, an ``Authorization`` header
in a library's error text. :class:`RedactingFilter` sits on every handler and
rewrites each record before it is written or shown:

* a password in an address (``https://user:pw@host``) becomes ``***``;
* the value of a query parameter or a ``name=value`` / ``"name": "value"``
  pair whose name says secret (password, token, key, signature...) becomes
  ``***``;
* a ``Bearer`` or ``Basic`` credential becomes ``***``;
* any value handed to :func:`hide` (the mail password and the notification
  webhook, from the settings) becomes ``***`` wherever it appears.

Enough of each line is left to see what happened and where.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Iterable

__all__ = ["secret_setting", "blank_secrets", "redact", "hide", "hide_settings",
           "RedactingFilter", "MASK"]

MASK = "***"

#: Settings whose values are secrets.
_SECRET_SETTING = re.compile(r"password|secret|token|api_key|apikey|webhook|credential",
                             re.IGNORECASE)


def secret_setting(key: str) -> bool:
    """Whether the setting *key* holds a secret. ``notify_webhook_format``
    names a body shape (json, ntfy, form), not the webhook, and is kept."""
    key = str(key)
    return bool(_SECRET_SETTING.search(key)) and not key.endswith("_format")


def blank_secrets(settings: dict[str, Any]) -> list[str]:
    """Empty every secret in *settings* in place; the names of those that held one."""
    blanked = []
    for key in list(settings):
        if secret_setting(key):
            if settings[key] not in ("", None):
                blanked.append(key)
            settings[key] = ""
    return sorted(blanked)


# -- the log -------------------------------------------------------------------------------

_NAMES = (r"password|passwd|passphrase|secret|token|api[_-]?key|apikey|access[_-]?key"
          r"|private[_-]?key|authorization|credential|cookie|signature")

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # https://user:password@host -> https://user:***@host
    (re.compile(r"(?P<lead>\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]*):[^/\s@'\"]+@", re.IGNORECASE),
     rf"\g<lead>:{MASK}@"),
    # Bearer abc.def / Basic dXNlcjpwdw==
    (re.compile(r"\b(?P<kind>Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE),
     rf"\g<kind> {MASK}"),
    # ?token=abc&x=1, &X-Amz-Signature=..., ?code=... (an OAuth code is a secret too)
    (re.compile(rf"(?P<lead>[?&;][\w.-]*(?:{_NAMES}|sig|code|key)[\w.-]*=)[^&\s#'\"]+",
                re.IGNORECASE),
     rf"\g<lead>{MASK}"),
    # password=abc, "refresh_token": "abc", api_key: abc, Authorization: xyz
    (re.compile(rf"(?P<lead>(?<![\w-])['\"]?[\w-]*(?:{_NAMES})['\"]?\s*[:=]\s*['\"]?)"
                rf"(?!\*\*\*)(?!Bearer\b|Basic\b)[^\s'\",;&}}\]]+", re.IGNORECASE),
     rf"\g<lead>{MASK}"),
)

_hidden_lock = threading.Lock()
_hidden: frozenset[str] = frozenset()

#: Shorter values are not hidden by value: "1234" would blot out every year.
_SHORTEST = 6


def hide(values: Iterable[Any]) -> None:
    """Hide these exact values wherever they appear in the log from now on,
    in place of the ones handed over before (the settings' secrets: set
    afresh whenever the settings are saved)."""
    global _hidden
    keep = frozenset(str(v) for v in values if isinstance(v, str) and len(v) >= _SHORTEST)
    with _hidden_lock:
        _hidden = keep


def hide_settings(cfg: Any) -> None:
    """:func:`hide` the secrets in a settings object (a Config): the mail
    password and the notification webhook, whose token is often in its path
    where no rule could recognise it."""
    import dataclasses                                   # noqa: PLC0415

    try:
        names = [f.name for f in dataclasses.fields(cfg)]
    except TypeError:
        names = [n for n in vars(cfg) if not n.startswith("_")]
    hide(getattr(cfg, name, "") for name in names if secret_setting(name))


def redact(text: str) -> str:
    """*text* with every secret the rules or :func:`hide` know of masked."""
    if not text:
        return text
    for value in sorted(_hidden, key=len, reverse=True):
        if value in text:
            text = text.replace(value, MASK)
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites each record's message and traceback with :func:`redact`.

    Put on a handler, not a logger: a logger's filter sees only records made
    on that logger, a handler's sees every record that reaches it. The record
    is changed in place, so every handler after this one sees it masked too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_ninaivu_redacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:                                # noqa: BLE001
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        record._ninaivu_redacted = True
        return True
