"""The Tamil font: Tamil Sangam MN where the device has it, else the Noto
Sans Tamil that only the Windows build carries.

Three places have to agree on one file name — tamil-font.css, the share page
(which has its own styles) and installers/windows/build.ps1, which fetches the
font — and the file must stay out of the repository. And because only one
build has the file, only that build may ask for it: every other build used to
request it on every page and log a 404 for it.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STYLE = ROOT / "ninaivu" / "static" / "css" / "style.css"
BUNDLED = ROOT / "ninaivu" / "static" / "css" / "tamil-font.css"
SHARE = ROOT / "ninaivu" / "static" / "css" / "share.css"
BUILD = ROOT / "installers" / "windows" / "build.ps1"


def _faces(text: str) -> list[str]:
    return re.findall(r"@font-face\s*\{(.*?)\}", text, re.S)


def test_both_pages_prefer_sangam_mn_then_the_bundled_file():
    faces = [f for f in _faces(BUNDLED.read_text(encoding="utf-8")) if '"Ninaivu Tamil"' in f]
    assert len(faces) == 2
    for face in faces:
        assert face.index("Tamil Sangam MN") < face.index("../fonts/NotoSansTamil.ttf")
        assert "U+0B80-0BFF" in face
    # The share and ask pages name the device's own face only; the bundled file
    # comes from tamil-font.css, which the page links where the build has it.
    for page in (SHARE, SHARE.with_name("ask.css")):
        faces = [f for f in _faces(page.read_text(encoding="utf-8")) if '"Ninaivu Tamil"' in f]
        assert len(faces) == 2, page.name
        for face in faces:
            assert "Tamil Sangam MN" in face and "url(" not in face, page.name
            assert "U+0B80-0BFF" in face, page.name


def test_the_tamil_family_leads_every_text_stack():
    assert re.search(r'--font:\s*"Ninaivu Tamil"', STYLE.read_text(encoding="utf-8"))
    assert re.search(r'font:\s*16px/1\.55\s*"Ninaivu Tamil"',
                     SHARE.read_text(encoding="utf-8"))


def test_the_windows_build_fetches_that_file_pinned_by_hash():
    build = BUILD.read_text(encoding="utf-8")
    assert 'Name = "NotoSansTamil.ttf"' in build
    assert 'Name = "NotoSansTamil-OFL.txt"' in build
    assert re.search(r'\$fontCommit = "[0-9a-f]{40}"', build)
    assert len(re.findall(r'Sha256 = "[0-9a-f]{64}"', build)) == 2
    # Before the Ninaivu wheel is built, or the wheel would not carry it.
    assert build.index("NotoSansTamil.ttf") < build.index("pip wheel")


def test_the_font_stays_out_of_the_repository():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "ninaivu/static/fonts/" in ignored


def test_the_main_stylesheet_never_names_the_file():
    """Every build loads style.css; only the Windows build has the font."""
    faces = [f for f in _faces(STYLE.read_text(encoding="utf-8")) if '"Ninaivu Tamil"' in f]
    assert len(faces) == 2
    assert all("Tamil Sangam MN" in f and "url(" not in f for f in faces)


@pytest.mark.parametrize("present", [False, True])
def test_the_pages_ask_for_the_file_only_where_the_build_has_it(app, as_family, tmp_path, present):
    static = tmp_path / "static"
    (static / "fonts").mkdir(parents=True)
    if present:
        (static / "fonts" / "NotoSansTamil.ttf").write_bytes(b"font")
    app.static_folder = str(static)
    page = as_family.get("/").get_data(as_text=True)
    assert ("/static/css/tamil-font.css" in page) is present
    rendered = render_share_page(app)
    assert ("/static/css/tamil-font.css" in rendered) is present
    assert "NotoSansTamil.ttf" not in rendered


def render_share_page(app):
    from flask import render_template

    with app.test_request_context("/s/x"):
        return render_template("share.html", token="x")
