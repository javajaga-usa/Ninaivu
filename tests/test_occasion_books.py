"""Photo books for occasions: picking, pages, the PDF, and who may have one.

A book is a copy of photographs to take away, so most of what matters is the
same as for a download: a family member's book never holds a photograph they
could not have found in the gallery, nor names anybody they could not find on
the People page, and a book is its maker's to download and delete.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu import build_services, create_home_app
from ninaivu.media import books
from ninaivu.server import auth
from ninaivu.storage import books as store


def _row(i: int, **extra):
    row = {"id": i, "kind": "picture", "captured_at": 1_700_000_000 + i * 600,
           "width": 4000, "height": 3000, "sharpness": 300.0, "quality": [],
           "phash": f"{random.Random(i).getrandbits(64):016x}", "dup_group": None}
    row.update(extra)
    return row


# -- picking ----------------------------------------------------------------

def test_blurry_screenshots_and_duplicates_stay_out():
    rows = [_row(i) for i in range(1, 41)]
    rows[2]["quality"] = ["blurry"]
    rows[3]["dup_group"] = rows[4]["dup_group"] = "g1"
    rows[4]["sharpness"] = 900.0                      # the better of the pair
    rows[10]["phash"] = rows[9]["phash"]              # a burst: same picture twice
    rows[10]["sharpness"] = 50.0
    picked = books.pick(rows, 40, screens={8})
    ids = [r["id"] for r in picked]
    assert 3 not in ids, "blurry"
    assert 8 not in ids, "a screenshot"
    assert 5 in ids and 4 not in ids, "one of a duplicate group, the better one"
    assert 10 in ids and 11 not in ids, "one of a burst, the sharper one"
    assert ids == sorted(ids, key=lambda i: rows[i - 1]["captured_at"]), "oldest first"


def test_as_many_as_asked_and_no_more():
    rows = [_row(i) for i in range(1, 101)]
    assert len(books.pick(rows, 36)) == 36
    assert len(books.pick(rows, 12)) == 12
    assert len(books.pick(rows[:7], 36)) == 7, "fewer than asked: all of them"
    picked = books.pick(rows, 36)
    times = [r["captured_at"] for r in picked]
    assert times == sorted(times)


def test_faces_are_preferred():
    rows = [_row(i) for i in range(1, 31)]
    with_faces = {1, 5, 9, 13, 17, 21, 25}
    picked = {r["id"] for r in books.pick(rows, 12, faces={i: 2 for i in with_faces})}
    assert with_faces <= picked


def test_the_whole_occasion_is_in_the_book_not_only_its_busiest_hour():
    burst = [_row(i, captured_at=1_700_000_000 + i, sharpness=2000.0) for i in range(1, 101)]
    later = [_row(1000 + i, captured_at=1_700_000_000 + 3600 * (i + 1)) for i in range(12)]
    picked = books.pick(burst + later, 12)
    assert sum(1 for r in picked if r["id"] >= 1000) >= 6


def test_about_one_in_six_has_a_page_of_its_own():
    picked = books.pick([_row(i) for i in range(1, 37)], 36)
    assert sum(r["hero"] for r in picked) == 6


# -- pages ------------------------------------------------------------------

@pytest.mark.parametrize("size, expected", [
    ("a4-portrait", (2480, 3508)), ("a4-landscape", (3508, 2480)),
    ("square-20", (2362, 2362)), ("square-30", (3543, 3543))])
def test_every_size_is_its_size_at_300_dpi(size, expected):
    assert books.page_pixels(size, bleed=False) == (*expected, 0)
    bleed = books.mm_px(3)
    assert books.page_pixels(size, bleed=True) == (expected[0] + 2 * bleed,
                                                   expected[1] + 2 * bleed, bleed)
    page = books.Book({"size": size, "bleed": True, "items": []}).closing()
    assert page.size == (expected[0] + 2 * bleed, expected[1] + 2 * bleed)


def _shaped(w, h, hero=False, i=0):
    return {"id": i, "width": w, "height": h, "hero": hero}


def test_layouts_follow_the_shape_of_the_photographs():
    portrait_page = books.Geometry("a4-portrait")
    landscape_page = books.Geometry("a4-landscape")
    two_landscapes = [_shaped(3000, 2000, i=1), _shaped(3000, 2000, i=2)]
    two_portraits = [_shaped(2000, 3000, i=1), _shaped(2000, 3000, i=2)]
    assert [p["layout"] for p in books.plan_pages(two_landscapes, portrait_page)] == ["2h"]
    assert [p["layout"] for p in books.plan_pages(two_portraits, landscape_page)] == ["2v"]
    four = [_shaped(3000, 2000, i=n) for n in range(4)]
    assert [p["layout"] for p in books.plan_pages(four, landscape_page)] == ["4"]
    with_hero = [_shaped(3000, 2000, i=1), _shaped(3000, 2000, True, 2), _shaped(3000, 2000, i=3)]
    plan = books.plan_pages(with_hero, portrait_page)
    assert [len(p["items"]) for p in plan] == [1, 1, 1]
    assert plan[1]["layout"] == "1" and plan[1]["items"][0]["id"] == 2
    # Nothing is lost or reordered.
    many = [_shaped(*random.Random(n).choice([(3000, 2000), (2000, 3000)]), i=n) for n in range(30)]
    flat = [item["id"] for page in books.plan_pages(many, portrait_page) for item in page["items"]]
    assert flat == list(range(30))


def test_every_frame_sits_inside_the_margins():
    for size in books.SIZES:
        geometry = books.Geometry(size, bleed=True, captions=True)
        x0, y0, x1, y1 = geometry.content
        for layout in ("1", *books.LAYOUTS):
            for a, b, c, d in geometry.frames(layout):
                assert x0 <= a < c <= x1 and y0 <= b < d <= y1


# -- rendering --------------------------------------------------------------

def _photos(folder: Path, n: int) -> list[dict]:
    items = []
    for i in range(n):
        w, h = (600, 400) if i % 3 else (400, 600)
        path = folder / f"p{i}.jpg"
        Image.new("RGB", (w, h), (40 * (i % 6), 120, 200)).save(path)
        items.append({"id": i + 1, "path": str(path), "width": w, "height": h,
                      "score": 1.0 + i / 10, "hero": i == 3, "when": 1_700_000_000 + i * 60,
                      "caption": "", "names": []})
    return items


def test_the_pdf_has_a_cover_the_pages_and_a_closing_page(tmp_path):
    items = _photos(tmp_path, 9)
    spec = {"title": "Pongal 2024", "subtitle": "14 January 2024", "template": "pongal",
            "size": "square-20", "bleed": True, "captions": True, "items": items}
    seen = []
    pages = books.render_book(spec, tmp_path / "book.pdf", report=lambda d, t: seen.append((d, t)))
    expected = len(books.plan_pages(items, books.Geometry("square-20", True, True))) + 2
    assert pages == expected
    data = (tmp_path / "book.pdf").read_bytes()
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page /Parent", data)) == expected
    assert b"/Count %d" % expected in data
    assert b"/TrimBox" in data, "a print shop finds the cut from the bleed"
    width, height, _ = books.page_pixels("square-20", bleed=True)
    assert len(re.findall(rb"/Width %d /Height %d" % (width, height), data)) == expected
    # Progress after every page, ending at the whole book.
    assert seen == [(n, expected) for n in range(1, expected + 1)]


def test_a_cancelled_book_leaves_no_file(tmp_path):
    spec = {"size": "square-20", "items": _photos(tmp_path, 4)}
    with pytest.raises(books.Cancelled):
        books.render_book(spec, tmp_path / "book.pdf", cancelled=lambda: True)
    assert not (tmp_path / "book.pdf").exists()


def test_a_photograph_is_printed_the_way_up_the_gallery_shows_it(tmp_path):
    path = tmp_path / "sideways.jpg"
    image = Image.new("RGB", (600, 400), (255, 0, 0))
    ImageDraw.Draw(image).rectangle((300, 0, 600, 400), fill=(0, 0, 255))
    image.save(path)
    turned = books._open_photo({"path": str(path), "rotation": 90}, (400, 600))
    assert turned.width < turned.height, "the index's quarter turn is applied"
    assert turned.getpixel((turned.width // 2, 10))[0] > 200, "red at the top after a clockwise turn"


def test_tamil_without_a_font_is_left_out_rather_than_printed_as_boxes():
    plain = books.Type({"latin": "", "latin_bold": "", "tamil": "", "tamil_bold": ""})
    assert plain.printable("பொங்கல் 2024") == "2024"
    assert plain.printable("Pongal 2024") == "Pongal 2024"
    assert books.text_runs("Maya, அம்மா") == [(False, "Maya, "), (True, "அம்மா")]


def test_a_tamil_title_is_drawn():
    found = books.fonts()
    if not found["tamil"]:
        pytest.skip("no Tamil font on this machine")
    assert books.covers(found["tamil"], "பொங்கல்")
    setter = books.Type(found)
    assert setter.width("பொங்கல்", 80) > 100

    def drawn(text):
        page = Image.new("L", (900, 200), 255)
        setter.draw(ImageDraw.Draw(page), 20, 150, text, 80, 0)
        return page

    title = drawn("பொங்கல்")
    assert title.getextrema()[0] < 100, "ink on the page"
    assert title.tobytes() != drawn("\U0010FFFD" * 5).tobytes(), "letters, not missing-glyph boxes"


# -- the family app ---------------------------------------------------------

@pytest.fixture()
def home(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.watch = False
    books.reset()
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                     role=auth.ROLE_GUEST, created_by=admin.id)
    auth.create_user(conn, "cousin", "cousin-password-1", display_name="Cousin",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, "visitor", "visitor-password-1", display_name="Visitor",
                     role=auth.ROLE_FAMILY, created_by=admin.id, scope="shared")
    conn.execute("UPDATE assets SET visibility=2 WHERE folder='private'")
    # The fixture's photographs are flat colours, whose perceptual hashes are
    # all alike; real ones are not.
    for (asset_id,) in conn.execute("SELECT id FROM assets").fetchall():
        conn.execute("UPDATE assets SET phash=?, dup_group=NULL, quality='[]' WHERE id=?",
                     (f"{random.Random(asset_id).getrandbits(64):016x}", asset_id))
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}
    arjun = conn.execute("INSERT INTO people_clusters(name, created_at) "
                         "VALUES('Arjun', 0)").lastrowid
    hidden = conn.execute("INSERT INTO people_clusters(name, created_at) "
                          "VALUES('Secret Uncle', 0)").lastrowid
    for asset, person in (("shot1.jpg", arjun), ("shot2.jpg", arjun), ("secret0.jpg", hidden),
                          ("secret0.jpg", arjun)):
        conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                     "VALUES(?, ?, 'confirmed', '[0,0,10,10]', x'00')", (ids[asset], person))
    conn.commit()
    services = build_services(cfg)
    services.scanner.stop()
    app = create_home_app(services)
    clients = {"family": login(app.test_client(), *FAMILY),
               "admin": login(app.test_client(), *ADMIN),
               "guest": login(app.test_client(), *GUEST),
               "cousin": login(app.test_client(), "cousin", "cousin-password-1"),
               "visitor": login(app.test_client(), "visitor", "visitor-password-1")}
    album = clients["admin"].post("/api/albums", json={
        "name": "Everything", "ids": sorted(ids.values())}).get_json()["id"]
    yield {"ids": ids, "conn": conn, "cfg": cfg, "app": app, "album": album,
           "arjun": arjun, "hidden": hidden, **clients}
    books.reset()
    services.stop(timeout=5.0)


def _pick(client, kind, sid, count=12):
    return client.post("/api/books/pick", json={"source": {"kind": kind, "id": sid}, "count": count})


def test_a_family_member_never_gets_admin_only_or_out_of_folder_photographs(home):
    secret = {home["ids"][f"secret{i}.jpg"] for i in range(3)}
    family = _pick(home["family"], "album", home["album"], 120)
    assert family.status_code == 200
    family_ids = {i["id"] for i in family.get_json()["items"]}
    assert family_ids and not family_ids & secret
    admin_ids = {i["id"] for i in _pick(home["admin"], "album", home["album"], 120)
                 .get_json()["items"]}
    assert secret & admin_ids, "an administrator's own book may have them"
    shared = {home["ids"][f"beach{i}.jpg"] for i in range(4)}
    visitor_ids = {i["id"] for i in _pick(home["visitor"], "album", home["album"], 120)
                   .get_json()["items"]}
    assert visitor_ids and visitor_ids <= shared, "only the folder they were given"


def test_building_cannot_widen_what_a_pick_showed(home, monkeypatch):
    made = {}
    monkeypatch.setattr(books, "start", lambda book_id, spec, **_: made.update(spec))
    secret = home["ids"]["secret0.jpg"]
    shot = home["ids"]["shot1.jpg"]
    response = home["family"].post("/api/books", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": [secret, shot],
        "captions": True, "title": "Ours"})
    assert response.status_code == 202
    assert [i["id"] for i in made["items"]] == [shot]
    assert made["items"][0]["names"] == ["Arjun"]


def test_names_are_only_of_people_the_viewer_can_see(home, monkeypatch):
    made = {}
    monkeypatch.setattr(books, "start", lambda book_id, spec, **_: made.update(spec))
    secret = home["ids"]["secret0.jpg"]
    assert home["admin"].post("/api/books", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": [secret],
        "captions": True}).status_code == 202
    assert made["items"][0]["names"] == ["Arjun", "Secret Uncle"]
    # Nobody the family member could not find on the People page is a source.
    assert _pick(home["family"], "person", home["hidden"]).status_code == 404
    picked = _pick(home["family"], "person", home["arjun"]).get_json()
    assert {i["id"] for i in picked["items"]} == {home["ids"]["shot1.jpg"], home["ids"]["shot2.jpg"]}
    assert picked["source"]["name"] == "Arjun"


def test_an_occasion_can_be_a_book(home):
    occasions = home["family"].get("/api/occasions").get_json()["occasions"]
    assert occasions
    picked = _pick(home["family"], "occasion", occasions[0]["id"])
    assert picked.status_code == 200 and picked.get_json()["items"]


def test_guests_make_no_books(home):
    assert _pick(home["guest"], "album", home["album"]).status_code in (401, 403)
    assert home["guest"].get("/api/books").status_code in (401, 403)
    assert home["guest"].post("/api/books", json={}).status_code in (401, 403)


def test_a_count_out_of_range_is_refused(home):
    assert _pick(home["family"], "album", home["album"], 5).status_code == 400
    assert _pick(home["family"], "album", home["album"], 500).status_code == 400
    assert home["family"].post("/api/books/pick", json={"source": {"kind": "folder", "id": 1}}
                               ).status_code == 400


def test_a_book_is_built_downloaded_by_its_owner_and_deleted(home):
    ids = [i["id"] for i in _pick(home["family"], "album", home["album"], 12).get_json()["items"]]
    made = home["family"].post("/api/books", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": ids, "template": "wedding",
        "size": "square-20", "title": "திருமணம் 2023", "captions": True})
    assert made.status_code == 202, made.get_json()
    book_id = made.get_json()["id"]
    assert books.wait(book_id, 120) == "done"
    status = home["family"].get(f"/api/books/{book_id}").get_json()
    assert status["state"] == "done" and status["pages"] >= 3
    assert status["progress"]["done"] == status["progress"]["total"] == status["pages"]
    listed = home["family"].get("/api/books").get_json()["books"]
    assert [b["id"] for b in listed] == [book_id]
    assert home["cousin"].get("/api/books").get_json()["books"] == []

    url = status["download"]
    got = home["family"].get(url)
    assert got.status_code == 200 and got.mimetype == "application/pdf"
    assert got.data.startswith(b"%PDF-")
    got.close()
    assert home["cousin"].get(url).status_code == 404, "somebody else's book"
    assert home["guest"].get(url).status_code in (401, 403)
    admin = home["admin"].get(url)
    assert admin.status_code == 200
    admin.close()

    path = store.file_of(home["cfg"].state_dir, store.get(home["conn"], book_id))
    assert path.is_file() and path.parent.name == "books"
    assert home["cousin"].delete(f"/api/books/{book_id}").status_code == 404
    assert path.is_file()
    assert home["family"].delete(f"/api/books/{book_id}").status_code == 200
    assert not path.exists()
    assert home["family"].get(f"/api/books/{book_id}").status_code == 404


def test_one_book_at_a_time(home, monkeypatch):
    monkeypatch.setattr(books, "building", lambda: True)
    ids = [home["ids"]["shot1.jpg"]]
    response = home["family"].post("/api/books", json={
        "source": {"kind": "album", "id": home["album"]}, "ids": ids})
    assert response.status_code == 409
    assert home["family"].get("/api/books").get_json()["books"] == []
