"""Reading the words in a photograph, and finding them again.

The engine is optional, so the tests split in two. Everything about the
database — the columns, the widened full-text index, the version stamp,
search actually matching — runs everywhere, driven by text written straight
into the row. Only the handful that need a real reader are skipped when the
package is absent.
"""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from ninaivu.media import ocr
from ninaivu.storage import db

needs_reader = pytest.mark.skipif(
    not ocr.available(), reason="the OCR package is not installed")


def sign(text_lines, size=(900, 320)):
    """A picture of some words, the way a shop sign is."""
    img = Image.new("RGB", size, (250, 250, 248))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
    except OSError:  # pragma: no cover - depends on the host's fonts
        font = ImageFont.load_default()
    for i, line in enumerate(text_lines):
        draw.text((40, 40 + i * 80), line, fill=(20, 20, 20), font=font)
    return img


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

def test_it_says_plainly_whether_it_can_read():
    assert isinstance(ocr.available(), bool)


def test_nothing_is_claimed_when_the_reader_is_missing(monkeypatch):
    """A household that never turns this on must never see it fail."""
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr, "_unavailable", True)
    assert ocr.read(sign(["anything"])) == {"text": "", "lines": 0, "score": 0.0}


def test_an_unreadable_file_is_not_a_crash():
    assert ocr.read("/nonexistent/photo.jpg")["text"] == ""


@needs_reader
def test_the_words_on_a_sign_come_back():
    result = ocr.read(sign(["NINAIVU BAKERY", "Open daily"]))
    assert "BAKERY" in result["text"].upper()
    assert result["lines"] >= 1
    assert 0 < result["score"] <= 1


@needs_reader
def test_a_photograph_with_no_words_reads_as_empty():
    assert ocr.read(Image.new("RGB", (800, 600), (90, 120, 70)))["text"] == ""


@needs_reader
def test_a_confidence_floor_of_one_keeps_nothing():
    """The floor is what stops foliage and brickwork entering the index."""
    assert ocr.read(sign(["NINAIVU BAKERY"]), min_score=1.01)["text"] == ""


@needs_reader
def test_the_text_kept_is_capped():
    """Cut on a line boundary — half a word helps nobody searching."""
    result = ocr.read(sign(["ONE TWO THREE", "FOUR FIVE SIX"]), max_chars=20)
    assert len(result["text"]) <= 20


# ---------------------------------------------------------------------------
# What the database does with it
# ---------------------------------------------------------------------------

def test_the_columns_are_there(scanned):
    _, conn, _ = scanned
    have = db.columns(conn, "assets")
    assert {"ocr_text", "ocr_version"} <= set(have)


def test_the_full_text_index_carries_the_words(scanned):
    _, conn, _ = scanned
    have = [row[1] for row in conn.execute("PRAGMA table_info(assets_fts)")]
    assert "ocr_text" in have


def test_searching_finds_a_photograph_by_what_is_written_in_it(as_family, scanned):
    _, conn, _ = scanned
    asset_id = conn.execute(
        "SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE assets SET ocr_text='NINAIVU BAKERY Mill Lane' WHERE id=?",
                 (asset_id,))
    conn.commit()

    found = as_family.get("/api/assets?q=bakery&limit=50").get_json()["items"]
    assert [item["id"] for item in found] == [asset_id]


def test_the_words_are_not_shown_as_a_caption(as_family, scanned):
    """It is what the photograph contains, not what it is about."""
    _, conn, _ = scanned
    asset_id = conn.execute(
        "SELECT id FROM assets WHERE kind='picture' LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE assets SET ocr_text='SOME SIGN' WHERE id=?", (asset_id,))
    conn.commit()
    body = as_family.get(f"/api/asset/{asset_id}").get_json()
    assert "SOME SIGN" not in (body.get("caption") or "")


def test_an_index_built_before_the_column_is_rebuilt(tmp_path):
    """An FTS5 table cannot be widened, so it has to be replaced.

    Left alone, the triggers would write six columns into a five-column index
    and every insert after the upgrade would fail.
    """
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE assets (
            id INTEGER PRIMARY KEY, root TEXT NOT NULL, rel_path TEXT NOT NULL,
            filename TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
            caption TEXT, camera TEXT, UNIQUE(root, rel_path));
        CREATE VIRTUAL TABLE assets_fts USING fts5(
            filename, folder, tags, caption, camera,
            content='assets', content_rowid='id', tokenize='unicode61');
    """)
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind) "
                 "VALUES ('r', 'a.jpg', 'seaside.jpg', 'picture')")
    conn.commit()
    conn.close()

    healed = db.init_db(path)
    have = [row[1] for row in healed.execute("PRAGMA table_info(assets_fts)")]
    assert "ocr_text" in have

    # …and the index was refilled from what was already indexed, rather than
    # being left empty for everything that came before the upgrade.
    hit = healed.execute(
        "SELECT rowid FROM assets_fts WHERE assets_fts MATCH 'seaside'").fetchone()
    assert hit is not None


def test_the_scan_pass_is_skipped_when_it_is_switched_off(scanned):
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.ocr_enabled = False
    conn.execute("UPDATE assets SET ocr_version=0")
    conn.commit()
    Scanner(cfg)._read_text(conn, cfg.active_root)
    still = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE ocr_version=0").fetchone()["n"]
    assert still > 0


def test_a_photograph_with_no_text_is_still_marked_read(scanned, monkeypatch):
    """Otherwise every scan reads the same wordless photograph forever."""
    from ninaivu.media import ocr as ocr_mod
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.ocr_enabled = True
    monkeypatch.setattr(ocr_mod, "available", lambda: True)
    monkeypatch.setattr(ocr_mod, "read",
                        lambda *a, **k: {"text": "", "lines": 0, "score": 0.0})
    conn.execute("UPDATE assets SET ocr_version=0")
    conn.commit()

    Scanner(cfg)._read_text(conn, cfg.active_root)
    unread = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE kind='picture' AND thumb IS NOT NULL "
        "AND ocr_version=0").fetchone()["n"]
    assert unread == 0


def test_what_was_read_lands_in_the_row_and_the_index(scanned, monkeypatch):
    from ninaivu.media import ocr as ocr_mod
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.ocr_enabled = True
    monkeypatch.setattr(ocr_mod, "available", lambda: True)
    monkeypatch.setattr(ocr_mod, "read", lambda *a, **k: {
        "text": "PLATFORM NINE", "lines": 1, "score": 0.9})
    conn.execute("UPDATE assets SET ocr_version=0")
    conn.commit()

    Scanner(cfg)._read_text(conn, cfg.active_root)
    assert conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE ocr_text='PLATFORM NINE'"
    ).fetchone()["n"] > 0
    assert conn.execute(
        "SELECT COUNT(*) n FROM assets_fts WHERE assets_fts MATCH 'platform'"
    ).fetchone()["n"] > 0


def test_the_switch_is_on_the_command_line():
    root = Path(__file__).resolve().parents[1]
    main = (root / "ninaivu" / "__main__.py").read_text(encoding="utf-8")
    assert '"--ocr"' in main and "cfg.ocr_enabled = True" in main


def test_either_package_shape_is_understood():
    """The reader was renamed, and the two versions answer differently.

    `rapidocr` returns an object with parallel txts/scores; the older
    `rapidocr_onnxruntime` returns (box, text, score) rows, or None. Flattening
    that here is what lets one household on 3.11 and another on 3.14 behave
    the same.
    """
    class NewShape:
        txts = ("SIGN", "SECOND LINE")
        scores = (0.9, 0.8)

    assert ocr._pairs(NewShape()) == [("SIGN", 0.9), ("SECOND LINE", 0.8)]
    assert ocr._pairs([([[0, 0]], "SIGN", 0.9)]) == [("SIGN", 0.9)]
    assert ocr._pairs(([([[0, 0]], "SIGN", 0.9)], 0.01)) == [("SIGN", 0.9)]
    assert ocr._pairs(None) == []


def test_a_malformed_answer_is_survived():
    assert ocr._pairs([("no score",), 42, None]) == []


def test_the_requirements_keep_opencv_below_five():
    """rapidocr asks for opencv with no ceiling, and 5.0 dropped the Haar
    cascades the orientation detector is built on. Installing OCR must not
    quietly break which-way-up detection."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "requirements-ocr.txt").read_text(encoding="utf-8")
    assert "opencv-python>=4.8,<5" in text
