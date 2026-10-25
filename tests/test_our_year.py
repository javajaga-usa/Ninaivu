"""Our Year: the family's year in photographs, as cards.

Off until switched on; counted through the viewer's own limits, so a hidden
photograph, a folder somebody is kept out of, or a person only seen in hidden
photographs never shows; a guest is told neither who nor where; the opening
words come from the on-device writing model when there is one, and are kept.
"""
from __future__ import annotations

import io
import json

import pytest

from ninaivu.api import api_our_year
from ninaivu.media import our_year
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS, OFF_THE_PAGE
from ninaivu.storage import db

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 400


@pytest.fixture()
def year(app, people, monkeypatch):
    """Every photograph moved into 2023, a month apart, with Paati in most
    and Arun, new this year, in a few; 2022 has one photograph of Paati."""
    monkeypatch.setattr(our_year, "MIN_ITEMS", 5)
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.our_year = True
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 ORDER BY id")]
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET sharpness=200, brightness=0.5")
        for n, aid in enumerate(ids[1:]):
            month = n % 12 + 1
            conn.execute("UPDATE assets SET date_key=?, captured_at=? WHERE id=?",
                         (f"2023-{month:02d}-{(n % 3) + 10:02d}", 1672531200 + n * 86400 * 30, aid))
        conn.execute("UPDATE assets SET date_key='2022-06-01', captured_at=1654041600 WHERE id=?",
                     (ids[0],))
        paati = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Paati', 0)").lastrowid
        arun = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Arun', 0)").lastrowid
        for aid in [ids[0], *ids[1:8]]:
            conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                         "VALUES (?, ?, 'confirmed', '[0,0,10,10]', x'00', 0.9)", (aid, paati))
        for aid in ids[4:8]:
            conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                         "VALUES (?, ?, 'confirmed', '[20,0,10,10]', x'00', 0.8)", (aid, arun))
        conn.executemany("UPDATE assets SET city='Ooty', country='India', gps_lat=11.41, gps_lon=76.69 "
                         "WHERE id=?", [(i,) for i in ids[1:4]])
        conn.execute("UPDATE assets SET city='Chennai', country='India', gps_lat=13.08, "
                     "gps_lon=80.27 WHERE id=?", (ids[5],))
        conn.commit()
    cfg.home_lat, cfg.home_lon = 13.08, 80.27
    return {"ids": ids, "paati": paati, "arun": arun, "conn": conn, "cfg": cfg}


def test_off_by_default_and_under_advanced():
    assert Config().our_year is True                 # on by default since 3.1
    assert "our_year" in GROUPS["Advanced"] and "our_year" in OFF_THE_PAGE


def test_every_route_is_404_until_switched_on(as_family, app):
    assert as_family.get("/api/our-year").status_code == 404
    assert as_family.get("/api/our-year/2023").status_code == 404
    assert as_family.post("/api/our-year/2023/words", json={}).status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["year"] is False


def test_the_years_there_is_enough_to_tell(as_family, year):
    got = as_family.get("/api/our-year").get_json()
    assert [y["year"] for y in got["years"]] == [2023]
    assert got["writer"] is False
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["year"] is True
    assert as_family.get("/api/our-year/2022").status_code == 404, "one photograph is not a year"


def test_the_year_in_cards(as_family, year):
    got = as_family.get("/api/our-year/2023").get_json()
    ids = year["ids"]
    assert got["year"] == 2023 and got["finished"] is True
    assert got["pictures"] == len(ids) - 1 and got["videos"] == 0
    assert sum(got["months"]) == got["items"] and got["busiest_month"] == 1
    assert got["previous_year"] == 1
    assert got["cover"]["id"] in ids
    assert len(got["month_photos"]) == len({m["month"] for m in got["month_photos"]}) >= 6
    people = got["people"]
    assert [p["name"] for p in people["top"]] == ["Paati", "Arun"]
    assert people["top"][0]["photos"] == 7 and people["top"][0]["face"]
    assert [p["name"] for p in people["new"]] == ["Arun"], "Paati was already there in 2022"
    assert people["together"]["photos"] == 4
    places = got["places"]
    assert places["top"][0] == {"place": "Ooty, India", "items": 3}
    assert places["countries"] == ["India"]
    assert places["furthest"]["place"] == "Ooty, India" and 400 < places["furthest"]["km"] < 460
    assert got["camera"]["name"]


def test_favourites_and_stories_are_counted(as_family, year):
    ids = year["ids"]
    assert as_family.post(f"/api/asset/{ids[3]}", json={"favorite": True}).status_code == 200
    told = as_family.post(f"/api/asset/{ids[2]}/stories",
                          data={"audio": (io.BytesIO(WEBM), "s.webm", "audio/webm"),
                                "text": "Paati at the lake in Ooty"},
                          content_type="multipart/form-data")
    assert told.status_code == 201
    got = our_year.summary(year["conn"], [year["cfg"].active_root],
                           {"viewer_id": 2, "max_visibility": 1, "scope": None}, 2023,
                           names={year["paati"]: "Paati"})
    assert got["favourites"]["loved"]["id"] == ids[3] and got["favourites"]["hearted"] == 1
    assert got["stories"]["count"] == 1
    assert got["stories"]["example"]["text"].startswith("Paati at the lake")


def test_nothing_hidden_counts(as_family, year):
    conn, ids = year["conn"], year["ids"]
    # Arun is only ever in hidden photographs now: he is neither named nor new.
    conn.executemany("UPDATE assets SET visibility=? WHERE id=?",
                     [(auth.VIS_HIDDEN, i) for i in ids[4:8]])
    conn.commit()
    got = our_year.summary(conn, [year["cfg"].active_root],
                           {"viewer_id": 2, "max_visibility": 1, "scope": None}, 2023,
                           names={year["paati"]: "Paati"})
    assert got["pictures"] == len(ids) - 5
    assert all(m["id"] not in ids[4:8] for m in got["month_photos"])
    assert [p["name"] for p in got["people"]["top"]] == ["Paati"]
    assert got["people"]["new"] == [] and got["people"]["together"] is None
    via_api = as_family.get("/api/our-year/2023").get_json()
    assert [p["name"] for p in via_api["people"]["top"]] == ["Paati"]


def test_a_guest_is_told_neither_who_nor_where(as_guest, year):
    year["conn"].execute("UPDATE assets SET visibility=?", (auth.VIS_PUBLIC,))
    year["conn"].commit()
    got = as_guest.get("/api/our-year/2023")
    assert got.status_code == 200
    body = got.get_json()
    assert body["people"] is None and body["places"] is None and body["stories"] is None
    assert all(not t["place"] for t in body["trips"])
    assert as_guest.post("/api/our-year/2023/words", json={}).status_code == 404


def test_without_the_writing_model_the_page_writes_its_own(as_family, year):
    got = as_family.post("/api/our-year/2023/words", json={"language": "ta"})
    assert got.status_code == 503 and got.get_json()["written"] == ""


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, capability, **kwargs):
        assert capability == "generate"
        self.calls.append(kwargs)
        return {"text": "**2023** was Paati's year. Arun joined us.", "model": "text-model"}


def test_the_opening_words_are_written_once_and_kept(as_family, year, monkeypatch):
    host = FakeHost()
    monkeypatch.setattr(api_our_year, "_writer", lambda: (True, ""))
    monkeypatch.setattr(api_our_year, "_host", lambda: host)
    api_our_year._words.clear()                                       # noqa: SLF001
    first = as_family.post("/api/our-year/2023/words", json={"language": "ta"}).get_json()
    assert first == {"written": "2023 was Paati's year. Arun joined us.", "model": "text-model",
                     "language": "ta"}
    again = as_family.post("/api/our-year/2023/words", json={"language": "ta"}).get_json()
    assert again == first and len(host.calls) == 1
    system, user = host.calls[0]["messages"]
    assert "Tamil" in system["content"] and "ONLY the facts" in system["content"]
    facts = json.loads(user["content"].split("\n", 1)[1])
    assert facts["new_to_the_family_photos"] == ["Arun"]
    assert "Ooty, India" in facts["places"]
    assert not any(str(i) in user["content"] for i in ("rel_path", "/lib/"))


def test_words_are_one_clean_paragraph():
    assert our_year.cleaned("## Hi\n\n*there*") == "Hi there"
    long = "One sentence here. " * 80
    cut = our_year.cleaned(long, limit=100)
    assert len(cut) <= 100 and cut.endswith(".")
