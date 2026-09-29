"""The Tamil font: Tamil Sangam MN where the device has it, else the Noto
Sans Tamil that only the Windows build carries.

Three places have to agree on one file name — the stylesheet, the share page
(which has its own styles) and installers/windows/build.ps1, which fetches the
font — and the file must stay out of the repository.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE = ROOT / "ninaivu" / "static" / "css" / "style.css"
SHARE = ROOT / "ninaivu" / "templates" / "share.html"
BUILD = ROOT / "installers" / "windows" / "build.ps1"


def _faces(text: str) -> list[str]:
    return re.findall(r"@font-face\s*\{(.*?)\}", text, re.S)


def test_both_pages_prefer_sangam_mn_then_the_bundled_file():
    for page, url in ((STYLE, "../fonts/NotoSansTamil.ttf"),
                      (SHARE, "/static/fonts/NotoSansTamil.ttf")):
        faces = [f for f in _faces(page.read_text(encoding="utf-8"))
                 if '"Ninaivu Tamil"' in f]
        assert len(faces) == 2, page.name
        for face in faces:
            assert face.index("Tamil Sangam MN") < face.index(url), page.name
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
