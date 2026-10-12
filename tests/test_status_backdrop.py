"""Safari on iOS 26 blurred the top of both apps: the logo, the buttons and
the avatar under the status bar. It takes the status bar's colour from an
element fixed at the top of the screen and blurs when there is none; both
pages carry one, the colour of their bars, as tall as the status bar."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_both_apps_carry_the_strip_first_thing_in_the_page():
    for page in ("index.html", "admin.html"):
        html = (ROOT / "ninaivu/templates" / page).read_text(encoding="utf-8")
        body = re.search(r"<body[^>]*>", html).end()
        assert '<div class="status-backdrop" aria-hidden="true"></div>' in html
        first = html[body:].split('<div class="status-backdrop"', 1)[0]
        assert "<" not in re.sub(r"<!--.*?-->", "", first, flags=re.S).strip(), page


def test_it_is_fixed_solid_and_only_where_there_is_a_status_bar():
    css = (ROOT / "ninaivu/static/css/style.css").read_text(encoding="utf-8")
    assert ".status-backdrop { display: none; }" in css
    rule = re.search(r"@media \(hover: none\), \(max-width: 1099px\) \{\s*\.status-backdrop \{[^}]*\}",
                     css).group(0)
    for needle in ("position: fixed", "top: 0", "env(safe-area-inset-top", "background: var(--bg-elev)",
                   "pointer-events: none", "6px)"):
        assert needle in rule
    # A transparent root counts as white to Safari.
    assert "html { height: 100%; background-color: var(--bg); }" in css
