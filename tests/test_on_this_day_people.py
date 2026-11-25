"""On this day is a memory of people, and the family app has a Documents view.

The user, 25 November 2026: On this day should not show screenshots or
near-duplicate photos, it should reflect people; a Documents section in the
family app's left pane holds only documents and screenshots; and the
console's Sign out button was out of its row.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _library(tmp_path):
    from ninaivu.server import auth
    from ninaivu.storage import db

    conn = db.init_db(tmp_path / "index.db")
    auth.init_auth_schema(conn)
    return db, conn


def _add(conn, rel_path, *, year, dup_group=None, faces=0, sharpness=100.0,
         visibility=1, vis_source=""):
    cur = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, kind, date_key, captured_at, mtime, "
        "thumb, visibility, vis_source, dup_group, sharpness, indexed_at) "
        "VALUES('/lib', ?, ?, 'picture', ?, ?, 0, ?, ?, ?, ?, ?, 0)",
        (rel_path, rel_path, f"{year}-03-14", (year - 1970) * 31_557_600 + 6_200_000,
         "ab/" + rel_path, visibility, vis_source,
         dup_group, sharpness))
    for _ in range(faces):
        conn.execute("INSERT INTO faces(asset_id, bbox, embedding) VALUES(?, '[0,0,1,1]', x'00')",
                     (cur.lastrowid,))
    return cur.lastrowid


def _on_day(db, conn, max_visibility=2):
    found = db.query_on_this_day(conn, ["/lib"], month=3, day=14,
                                 max_visibility=max_visibility)
    return [item["rel_path"] for item in found["items"]], found


def test_screenshots_and_documents_are_left_out_for_everyone(tmp_path):
    db, conn = _library(tmp_path)
    _add(conn, "beach.jpg", year=2015, faces=2)
    # Shown to the family by hand, as 285 WhatsApp screenshots were.
    _add(conn, "Screenshot_20150314.png", year=2015, vis_source="item")
    _add(conn, "receipt.jpg", year=2016, visibility=2, vis_source="screen")
    conn.commit()

    for ceiling in (1, 2):
        names, _ = _on_day(db, conn, ceiling)
        assert names == ["beach.jpg"], (ceiling, names)


def test_a_near_duplicate_group_is_shown_once_by_its_best_frame(tmp_path):
    db, conn = _library(tmp_path)
    _add(conn, "burst-1.jpg", year=2012, dup_group="g1", faces=1, sharpness=50)
    _add(conn, "burst-2.jpg", year=2012, dup_group="g1", faces=3, sharpness=40)
    _add(conn, "burst-3.jpg", year=2012, dup_group="g1", faces=3, sharpness=90)
    _add(conn, "other.jpg", year=2012, dup_group="g2", faces=1)
    conn.commit()

    names, found = _on_day(db, conn)
    assert sorted(names) == ["burst-3.jpg", "other.jpg"]
    assert found["total"] == 2
    assert sum(len(v) for v in found["years"].values()) == 2


def test_photographs_of_people_come_first(tmp_path):
    db, conn = _library(tmp_path)
    # Newest first within each half: 2020 sunset, then the 2010 one.
    _add(conn, "sunset-2020.jpg", year=2020)
    _add(conn, "family-2011.jpg", year=2011, faces=4)
    _add(conn, "sunset-2010.jpg", year=2010)
    _add(conn, "grandma-2018.jpg", year=2018, faces=1)
    conn.commit()

    names, found = _on_day(db, conn)
    assert names == ["grandma-2018.jpg", "family-2011.jpg",
                     "sunset-2020.jpg", "sunset-2010.jpg"]
    assert "face_count" not in found["items"][0]


def test_documents_view_lists_only_documents_and_screenshots(tmp_path):
    db, conn = _library(tmp_path)
    _add(conn, "beach.jpg", year=2015, faces=2)
    _add(conn, "Screenshot_1.png", year=2015)
    _add(conn, "passport.jpg", year=2016, visibility=2, vis_source="screen")
    conn.commit()

    rows, total = db.query_assets(conn, ["/lib"], max_visibility=2, screens_only=True)
    assert sorted(r["rel_path"] for r in rows) == ["Screenshot_1.png", "passport.jpg"]
    assert total == 2
    # A family member sees none of the hidden ones.
    rows, _ = db.query_assets(conn, ["/lib"], max_visibility=1, screens_only=True)
    assert [r["rel_path"] for r in rows] == ["Screenshot_1.png"]

    assert db.library_stats(conn, ["/lib"], max_visibility=2)["documents"] == 2
    assert db.library_stats(conn, ["/lib"], max_visibility=1)["documents"] == 0


def test_the_sidebar_groups_its_views_and_offers_documents_to_administrators():
    page = (ROOT / "ninaivu" / "templates" / "index.html").read_text(encoding="utf-8")
    nav = page[page.index('aria-label="Views"'):page.index("</nav>", page.index('aria-label="Views"'))]
    order = re.findall(r'data-view="([a-z]+)"|data-i18n="(Library|Memories|Tidy up)"', nav)
    flat = [view or heading for view, heading in order]
    assert flat == ["Library", "all", "pictures", "videos", "live", "audio",
                    "Memories", "memories", "favorites", "private",
                    "Tidy up", "documents", "duplicates", "hidden", "public"]
    assert re.search(r'class="side-item admin-only" data-view="documents"', nav)
    # Years sit with the other ways in by date, before the AI's tags.
    assert page.index('id="years-block"') < page.index('id="tags-block"')
    app = (ROOT / "ninaivu" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "if (state.view === 'documents') filters.screens = true;" in app
    http = (ROOT / "ninaivu" / "static" / "js" / "http.js").read_text(encoding="utf-8")
    assert "if (filters.screens) params.set('screens', '1');" in http


def test_the_new_words_are_in_tamil():
    import json

    ta = json.loads((ROOT / "ninaivu" / "static" / "i18n" / "ta.json").read_text(encoding="utf-8"))
    for key in ("Documents", "Tidy up", "No documents or screenshots"):
        assert ta.get(key) and ta[key] != key, key


def test_the_consoles_round_buttons_share_one_row_under_family_app():
    css = (ROOT / "ninaivu" / "static" / "css" / "admin.css").read_text(encoding="utf-8")
    assert "display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));" in css
    assert ".admin-bar-actions a.btn { grid-column: 1 / -1; width: 100%; }" in css
    # Sign out was pushed to the right of a row of its own.
    assert ".admin-bar-actions .icon-btn:last-child { margin-left: auto; }" not in css
