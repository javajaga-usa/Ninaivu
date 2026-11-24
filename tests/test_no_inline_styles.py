"""The page's policy refuses inline styles, so none may be written.

``style-src 'self'`` (ninaivu/__init__.py) allows a stylesheet from this
server and nothing inline. A ``style="..."`` attribute in a template, or in
HTML a script builds, is silently ignored by the browser under that policy:
the page looks broken and no test notices. So they are looked for here.

Setting a style from a script through the style object (``el.style.width =``,
``el.style.setProperty(...)``) is allowed and is how a value that is only
known at run time is applied.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "ninaivu"
STYLE_ATTR = re.compile(r'''(?<![.\w-])style=(["'])''')
STYLE_BLOCK = re.compile(r"<style[\s>]")

#: Documents Ninaivu writes for somebody to save and open from a file: the
#: policy of this server never reaches them.
SAVED_DOCUMENTS = {
    "static/js/creative-studio.js",      # the exported album page and print layout
    "storage/keepsake.py",               # the keepsake web page
    "archive/status_kit.py",             # the archive's status page, written to the disk
    "utils/digest.py",                   # the weekly e-mail
    "server/csp.py",                     # says how a <style> block gets its nonce
}


def _sources():
    for sub, pattern in (("templates", "*.html"), ("static/js", "*.js"), ("static/css", "*.css"), ("", "*.py")):
        for path in (ROOT / sub).rglob(pattern):
            rel = path.relative_to(ROOT).as_posix()
            if rel in SAVED_DOCUMENTS or "/vendor/" in rel or rel.startswith(("static/js/leaflet", "desktop/")):
                continue
            yield rel, path


def test_no_page_writes_an_inline_style():
    found = []
    for rel, path in _sources():
        if rel.endswith(".css"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if STYLE_ATTR.search(line) or (STYLE_BLOCK.search(line) and "style_attr()" not in line):
                found.append(f"{rel}:{number}: {line.strip()[:100]}")
    assert not found, "inline style (use a class in a stylesheet):\n" + "\n".join(found[:20])


def test_the_policy_names_no_unsafe_inline_for_styles():
    text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "style-src 'self' 'unsafe-inline'" not in text


def test_the_header_the_pages_receive_allows_only_the_servers_own_styles(as_family):
    policy = as_family.get("/").headers["Content-Security-Policy"]
    assert "style-src 'self';" in policy
    assert "unsafe-inline" not in policy


def test_a_page_that_asks_for_a_nonce_gets_it_named_in_the_policy(app):
    from ninaivu.server import csp

    with app.test_request_context("/"):
        nonce = csp.style_nonce()
        assert nonce and csp.style_nonce() == nonce        # one per request
        assert f'<style nonce="{nonce}">' in f"<style{csp.style_attr()}>"
        assert f"'nonce-{nonce}'" in csp.style_src()
    assert csp.style_attr() == "" and csp.style_src() == "style-src 'self'; "
