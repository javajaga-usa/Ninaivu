"""Festival albums: every Deepavali, every Pongal, side by side.

The festival days come from the Tamil calendar worked out from the sun and
the moon, checked here against the days families kept; off until switched
on; a year's photographs are the days around the festival, kept only where
the picture search says they look like it (or, without it, on their dates
alone); a date the camera did not record proves nothing; read through the
viewer's own limits, so family members never see hidden photographs and
administrators see everything; a book can be made of every year.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ninaivu.api import api_festivals
from ninaivu.media import festival_calendar as calendar
from ninaivu.media import festivals
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db

# The days Tamil families kept (Tamil Nadu's own calendar where India's differs).
KNOWN = {
    "pongal": ["2015-01-15", "2016-01-15", "2017-01-14", "2019-01-15", "2020-01-15", "2021-01-14",
               "2022-01-14", "2023-01-15", "2024-01-15", "2025-01-14", "2026-01-14"],
    "deepavali": ["2015-11-10", "2016-10-29", "2017-10-18", "2018-11-06", "2019-10-27", "2020-11-14",
                  "2021-11-04", "2022-10-24", "2023-11-12", "2024-10-31", "2025-10-20"],
    "karthigai": ["2015-11-25", "2016-12-12", "2017-12-02", "2019-12-10", "2020-11-29", "2021-11-19",
                  "2022-12-06", "2023-11-26", "2024-12-13", "2025-12-03"],
    "vinayagar": ["2016-09-05", "2017-08-25", "2018-09-13", "2019-09-02", "2020-08-22", "2021-09-10",
                  "2022-08-31", "2024-09-07", "2025-08-27"],
    "puthandu": ["2016-04-14", "2018-04-14", "2020-04-14", "2022-04-14", "2023-04-14", "2025-04-14"],
}
NAVARATRI_FIRST = ["2016-10-01", "2017-09-21", "2018-10-10", "2019-09-29", "2020-10-17",
                   "2021-10-07", "2022-09-26", "2023-10-15", "2024-10-03", "2025-09-22"]


@pytest.mark.parametrize("key", sorted(KNOWN))
def test_the_festival_days_from_the_sky(key):
    for known in KNOWN[key]:
        day = date.fromisoformat(known)
        assert calendar.days(key, day.year)[0] == day, (key, known)


def test_navaratri_from_its_first_day_to_vijayadashami():
    for known in NAVARATRI_FIRST:
        first = date.fromisoformat(known)
        start, tenth = calendar.navaratri(first.year)
        assert start == first, known
        assert 8 <= (tenth - start).days <= 10
    assert calendar.navaratri(2024)[1] == date(2024, 10, 12)


def test_a_day_on_the_edge_is_inside_the_album():
    """Karthigai Deepam 2018 was kept on 23 November, a day from the rule's
    answer; Vinayagar Chathurthi 2023 on the 18th of September: the album's
    days around the festival still hold them."""
    for key, kept in (("karthigai", date(2018, 11, 23)), ("vinayagar", date(2023, 9, 18)),
                      ("krishna", date(2023, 9, 6))):
        _, first, last = calendar.days(key, kept.year)
        assert first <= kept <= last, key


def test_the_sun_and_the_moon_where_meeus_puts_them():
    sun = calendar._jd(datetime(1992, 10, 13, tzinfo=timezone.utc))      # noqa: SLF001
    assert abs(calendar.sun_longitude(sun) - 199.909) < 0.01
    moon = calendar._jd(datetime(1992, 4, 12, tzinfo=timezone.utc))     # noqa: SLF001
    assert abs(calendar.moon_longitude(moon) - 133.167) < 0.02


def test_off_by_default_and_under_advanced():
    assert Config().festival_albums is False
    assert "festival_albums" in GROUPS["Advanced"]


def test_every_route_is_404_until_switched_on(as_family):
    assert as_family.get("/api/festivals").status_code == 404
    assert as_family.get("/api/festivals/deepavali").status_code == 404
    assert as_family.post("/api/books/pick", json={"source": {"kind": "festival", "id": 6},
                                                   "count": 12}).status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["festivals"] is False


@pytest.fixture()
def lit(app, people):
    """Deepavali in 2019 (27 October) and 2023 (12 November): three photographs
    each day, one more in 2023 the day after; and the rest on ordinary days."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.festival_albums = True
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 ORDER BY id")]
    assert len(ids) >= 10
    dates = (["2019-10-27"] * 3 + ["2023-11-12"] * 3 + ["2023-11-13"]
             + [f"2021-0{n % 8 + 1}-0{n % 8 + 1}" for n in range(len(ids) - 7)])
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET sharpness=200, brightness=0.5, date_source='exif'")
        for aid, key in zip(ids, dates):
            conn.execute("UPDATE assets SET date_key=? WHERE id=?", (key, aid))
        conn.commit()
    viewer = {"viewer_id": people["family"].id, "max_visibility": 1, "scope": None}
    return {"ids": ids, "conn": conn, "cfg": cfg, "viewer": viewer,
            "deepavali_2019": ids[:3], "deepavali_2023": ids[3:6], "day_after": ids[6]}


def _albums(lit, **kw):
    kw.setdefault("today", date(2026, 10, 22))
    return festivals.albums(lit["conn"], [lit["cfg"].active_root], lit["viewer"], **kw)


def _festival(found, key):
    return next(f for f in found["festivals"] if f["key"] == key)


def test_found_on_their_dates_without_the_picture_search(lit, monkeypatch):
    monkeypatch.setattr(festivals, "MIN_BY_DATE", 3)
    deepavali = _festival(_albums(lit), "deepavali")
    assert [y["year"] for y in deepavali["years"]] == [2019, 2023]
    first, second = deepavali["years"]
    assert first["day"] == "2019-10-27" and not first["checked"]
    assert set(first["ids"]) == set(lit["deepavali_2019"])
    assert set(second["ids"]) == {*lit["deepavali_2023"], lit["day_after"]}
    assert deepavali["first"] == 2019 and deepavali["last"] == 2023 and deepavali["items"] == 7
    assert deepavali["cover"]["id"] in second["ids"]
    assert deepavali["next"] == "2026-11-08"
    assert _festival(_albums(lit), "pongal")["years"] == []


def test_too_few_on_a_date_is_not_a_festival(lit):
    """Three photographs on 27 October 2019 are not enough on dates alone."""
    deepavali = _festival(_albums(lit), "deepavali")
    assert [y["year"] for y in deepavali["years"]] == [2023]


def _check(festive: set[int], read: set[int]):
    def check(key, ids):
        assert key in festivals.FESTIVALS
        return {i: (0.05 if i in festive else -0.05) for i in ids if i in read}
    return check


def test_the_picture_search_keeps_only_the_festival_days(lit):
    """2019's photographs look like an office, not Deepavali; in 2023 one
    lamp on the 12th keeps that whole day, and the 13th, with nothing
    festive, is left out."""
    every = set(lit["ids"])
    lamp = lit["deepavali_2023"][1]
    deepavali = _festival(_albums(lit, check=_check({lamp}, every)), "deepavali")
    assert [y["year"] for y in deepavali["years"]] == [2023]
    year = deepavali["years"][0]
    assert year["checked"] and year["ids"][0] == lamp
    assert set(year["ids"]) == set(lit["deepavali_2023"])
    assert year["photos"][0]["id"] == lamp


def test_photographs_not_read_yet_join_on_their_dates(lit):
    lamp = lit["deepavali_2023"][0]
    read = {lamp, lit["day_after"]}
    year = _festival(_albums(lit, check=_check({lamp}, read)), "deepavali")["years"][-1]
    assert set(year["ids"]) == set(lit["deepavali_2023"]), "the 13th was read and is not festive"


def test_a_date_the_camera_did_not_record_proves_nothing(lit, monkeypatch):
    monkeypatch.setattr(festivals, "MIN_BY_DATE", 1)
    lit["conn"].executemany("UPDATE assets SET date_source='mtime' WHERE id=?",
                            [(i,) for i in lit["deepavali_2019"]])
    lit["conn"].commit()
    years = _festival(_albums(lit), "deepavali")["years"]
    assert [y["year"] for y in years] == [2023]


def test_hidden_photographs_are_for_administrators_only(lit, monkeypatch):
    monkeypatch.setattr(festivals, "MIN_BY_DATE", 1)
    hidden = lit["deepavali_2019"]
    lit["conn"].executemany("UPDATE assets SET visibility=? WHERE id=?",
                            [(auth.VIS_HIDDEN, i) for i in hidden])
    lit["conn"].commit()
    family = _festival(_albums(lit), "deepavali")
    assert [y["year"] for y in family["years"]] == [2023]
    assert not set(hidden) & set(festivals.ids_of(_albums(lit), "deepavali"))
    admin = dict(lit["viewer"], max_visibility=auth.VIS_HIDDEN)
    seen = festivals.albums(lit["conn"], [lit["cfg"].active_root], admin, today=date(2026, 10, 22))
    assert set(hidden) <= set(festivals.ids_of(seen, "deepavali"))


def test_explicit_photographs_only_for_an_administrator(as_family, as_admin, lit, monkeypatch):
    monkeypatch.setattr(festivals, "MIN_BY_DATE", 1)
    monkeypatch.setattr(api_festivals, "_check", lambda conn: None)
    adult = lit["deepavali_2023"][0]
    lit["conn"].execute("UPDATE assets SET nsfw=1 WHERE id=?", (adult,))
    lit["conn"].commit()
    family = as_family.get("/api/festivals/deepavali").get_json()
    assert adult not in [i for y in family["years"] for i in y["ids"]]
    admin = as_admin.get("/api/festivals/deepavali").get_json()
    assert adult in [i for y in admin["years"] for i in y["ids"]]


def test_the_api(as_family, as_guest, lit, monkeypatch):
    monkeypatch.setattr(festivals, "MIN_BY_DATE", 3)
    monkeypatch.setattr(api_festivals, "_check", lambda conn: None)
    listed = as_family.get("/api/festivals").get_json()
    assert [f["key"] for f in listed["festivals"]] == list(calendar.FESTIVALS)
    deepavali = _festival(listed, "deepavali")
    assert deepavali["index"] == festivals.index_of("deepavali")
    assert [y["year"] for y in deepavali["years"]] == [2019, 2023]
    assert "ids" not in deepavali["years"][0] and "photos" not in deepavali["years"][0]
    one = as_family.get("/api/festivals/deepavali").get_json()
    assert one["years"][0]["ids"] and one["years"][0]["photos"][0]["thumb_v"] is not None
    assert as_family.get("/api/festivals/holi").status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["festivals"] is True
    # A guest may look, through their own limits: these photographs are the family's.
    guest = as_guest.get("/api/festivals/deepavali")
    assert guest.status_code == 200 and guest.get_json()["years"] == []


def test_the_picture_search_compares_with_ordinary_photographs(lit, app, monkeypatch):
    """The check scores each photograph's vector against the festival's
    words and the ordinary ones; ids without a vector are not scored."""
    np = pytest.importorskip("numpy")
    conn = lit["conn"]
    festive_dir = np.zeros(4, dtype="float32")
    festive_dir[0] = 1.0
    plain_dir = np.zeros(4, dtype="float32")
    plain_dir[1] = 1.0
    lamp, office = lit["deepavali_2023"][:2]
    db.store_embedding(conn, lamp, "test", 4, (festive_dir * 0.9 + plain_dir * 0.1).tobytes())
    db.store_embedding(conn, office, "test", 4, (plain_dir * 0.9 + festive_dir * 0.1).tobytes())
    ordinary = set(festivals.ORDINARY)

    class Engine:
        semantic = True
        model_id = "test"

        def encode_text(self, text):
            return plain_dir if text in ordinary else festive_dir

    monkeypatch.setitem(app.config, "MV_ENGINE", Engine())
    with app.test_request_context():
        check = api_festivals._check(conn)                            # noqa: SLF001
        margins = check("deepavali", [lamp, office, lit["day_after"]])
    assert margins[lamp] > 0 > margins[office]
    assert lit["day_after"] not in margins
    db.forget_embeddings()


def test_a_book_of_every_year(as_family, lit, monkeypatch):
    monkeypatch.setattr(festivals, "MIN_BY_DATE", 3)
    monkeypatch.setattr(api_festivals, "_check", lambda conn: None)
    got = as_family.post("/api/books/pick", json={
        "source": {"kind": "festival", "id": festivals.index_of("deepavali")}, "count": 12})
    assert got.status_code == 200, got.get_json()
    body = got.get_json()
    assert body["source"]["name"] == "Deepavali"
    wanted = set(lit["deepavali_2019"]) | set(lit["deepavali_2023"]) | {lit["day_after"]}
    assert {i["id"] for i in body["items"]} <= wanted
    assert as_family.post("/api/books/pick", json={
        "source": {"kind": "festival", "id": 99}, "count": 12}).status_code == 404
