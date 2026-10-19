"""The FriBiDi the portable builds carry, found before Pillow looks for it.

ninaivu/utils/fribidi.py. Pillow turns raqm (and with it Tamil) off when it
cannot find FriBiDi the first time its text layout is imported; the Mac and
Windows builds carry a copy beside their own Python.
"""

from __future__ import annotations

from ninaivu.utils import fribidi


def test_each_build_carries_it_where_pillow_looks(tmp_path):
    (tmp_path / "lib").mkdir()
    assert fribidi.bundled("win32", str(tmp_path)) is None
    assert fribidi.bundled("darwin", str(tmp_path)) is None
    (tmp_path / "libfribidi-0.dll").write_bytes(b"")
    (tmp_path / "lib" / "libfribidi.dylib").write_bytes(b"")
    assert fribidi.bundled("win32", str(tmp_path)) == tmp_path / "libfribidi-0.dll"
    assert fribidi.bundled("darwin", str(tmp_path)) == tmp_path / "lib" / "libfribidi.dylib"
    # Linux has FriBiDi as part of its desktop: nothing is carried.
    assert fribidi.bundled("linux", str(tmp_path)) is None


def test_once_pillow_has_looked_there_is_nothing_to_do(monkeypatch):
    import PIL._imagingft  # noqa: F401
    called = []
    monkeypatch.setattr(fribidi, "bundled", lambda *a, **k: called.append(1))
    assert fribidi.make_ready() is False
    assert called == []
