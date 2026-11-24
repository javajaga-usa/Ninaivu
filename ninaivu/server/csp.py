"""The one place a page may carry a <style> block of its own.

The Content-Security-Policy names ``style-src 'self'`` and nothing else, so a
style attribute or a <style> block is refused. A few pages are built by the
server around a stylesheet that belongs to that one page (the handover sheet
and the maintenance notice); each asks for a nonce here, writes it on its
<style> tag, and the policy header, written after the response is made, adds
that one nonce to ``style-src``. Nothing else gains anything.
"""
from __future__ import annotations

import secrets

from flask import g, has_request_context


def style_nonce() -> str:
    """This request's style nonce, made on first use. Empty outside a request."""
    if not has_request_context():
        return ""
    if not getattr(g, "_style_nonce", None):
        g._style_nonce = secrets.token_urlsafe(18)
    return g._style_nonce


def style_attr() -> str:
    """`` nonce="..."`` for a <style> tag, or nothing outside a request."""
    nonce = style_nonce()
    return f' nonce="{nonce}"' if nonce else ""


def style_src() -> str:
    """The ``style-src`` directive: the page's own files, plus this request's nonce if one was asked for."""
    nonce = getattr(g, "_style_nonce", None) if has_request_context() else None
    return "style-src 'self'" + (f" 'nonce-{nonce}'" if nonce else "") + "; "
