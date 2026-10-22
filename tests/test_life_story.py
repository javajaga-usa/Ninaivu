"""A life in photographs: one person's life told decade by decade.

Off until switched on; for the family only; read through the viewer's own
limits, so a hidden photograph or a person only seen in hidden photographs
never shows; chapters by decade with the best photographs, places, who was
beside them and who joined their photographs, children born from the family
tree, and the stories told; the chapters' words come from the on-device
writing model when there is one, and are kept for exactly the facts they
were written from.
"""
from __future__ import annotations

import io
import json

import pytest

from ninaivu.api import api_life_story
from ninaivu.media import life_story
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, family_tree, life_story as words_store, then_and_now as born_store

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 400


def _face(conn, aid, pid, x=0):
    conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                 "VALUES (?, ?, 'confirmed', ?, x'00', 0.9)", (aid, pid, f"[{x},0,10,10]"))


@pytest.fixture()
def lived(app, people, monkeypatch):
    """Paati in every photograph: three in 1975 with Thatha (her husband),
    four in 1988 with Amma (her daughter, born 1978), the rest in 2015-2019
    with Amma; one 1999 photograph on its own joins the decade after it."""
    monkeypatch.setattr(life_story, "MIN_ITEMS", 5)
    monkeypatch.setattr(life_story, "MIN_CHAPTER", 2)
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.life_story = True
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE kind='picture' AND live_clip=0 AND raw_twin=0 ORDER BY id")]
    assert len(ids) >= 10
    dates = (["1975-03-01", "1975-03-02", "1976-05-09"] + ["1988-01-10", "1988-01-11", "1988-04-02",
             "1989-12-25"] + ["1999-06-01"]
             + [f"{2015 + n % 5}-0{n % 9 + 1}-1{n % 9}" for n in range(len(ids) - 8)])
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET sharpness=200, brightness=0.5")
        for n, (aid, key) in enumerate(zip(ids, dates)):
            conn.execute("UPDATE assets SET date_key=?, captured_at=?, city=NULL, country=NULL "
                         "WHERE id=?", (key, 100000000 + n * 86400 * 40, aid))
        paati = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Paati', 0)").lastrowid
        thatha = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Thatha', 0)").lastrowid
        amma = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Amma', 0)").lastrowid
        for aid in ids:
            _face(conn, aid, paati)
        for aid in ids[:3]:
            _face(conn, aid, thatha, 20)
        for aid in ids[3:7] + ids[8:]:
            _face(conn, aid, amma, 40)
        conn.executemany("UPDATE assets SET city='Madurai', country='India' WHERE id=?",
                         [(i,) for i in ids[:3]])
        conn.commit()
    family_tree.add(conn, thatha, paati, "spouse", None)
    family_tree.add(conn, paati, amma, "parent", None)
    born_store.set_born(conn, paati, 1950)
    born_store.set_born(conn, amma, 1978)
    viewer = {"viewer_id": people["family"].id, "max_visibility": 1, "scope": None}
    return {"ids": ids, "paati": paati, "thatha": thatha, "amma": amma, "conn": conn, "cfg": cfg,
            "viewer": viewer}


def _life(lived, **kw):
    conn = lived["conn"]
    names = {lived["paati"]: "Paati", lived["thatha"]: "Thatha", lived["amma"]: "Amma"}
    names.update(kw.pop("names", {}))
    return life_story.life(conn, [lived["cfg"].active_root], lived["viewer"], lived["paati"],
                           names={k: v for k, v in names.items() if v}, born=born_store.born(conn),
                           links=family_tree.relations(conn), **kw)


def test_off_by_default_and_under_advanced():
    assert Config().life_story is False
    assert "life_story" in GROUPS["Advanced"]


def test_every_route_is_404_until_switched_on(as_family, app):
    assert as_family.get("/api/life").status_code == 404
    assert as_family.get("/api/life/1").status_code == 404
    assert as_family.post("/api/life/1/words", json={}).status_code == 404
    assert as_family.put("/api/life/1/born", json={"year": 1950}).status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["life"] is False


def test_the_chapters_of_a_life(lived):
    got = _life(lived)
    ids = lived["ids"]
    person = got["person"]
    assert person["name"] == "Paati" and person["born"] == 1950
    assert person["photos"] == len(ids) and person["first_year"] == 1975
    chapters = got["chapters"]
    assert [(c["start"], c["end"]) for c in chapters] == [(1970, 1979), (1980, 1989), (1990, 2019)]
    first, second, third = chapters
    assert first["ages"] == [25, 26] and first["items"] == 3
    assert first["places"] == [{"place": "Madurai, India", "items": 3}]
    assert [(p["name"], p["relation"]) for p in first["new"]] == [("Thatha", "spouse")]
    assert [(p["name"], p["relation"]) for p in first["with"]] == [("Thatha", "spouse")]
    assert [(p["name"], p["year"]) for p in first["born"]] == [("Amma", 1978)]
    assert [(p["name"], p["relation"]) for p in second["new"]] == [("Amma", "child")]
    assert third["first_year"] == 1999, "the lone 1999 photograph joined the decade after it"
    assert third["new"] == [], "Amma joined in the 1980s"
    assert all(1 <= len(c["photos"]) <= life_story.PHOTOS for c in chapters)
    assert {p["id"] for c in chapters for p in c["photos"]} <= set(ids)
    assert got["cover"]["id"] in ids and got["first"]["id"] == ids[0]


def test_not_enough_is_no_story(lived, monkeypatch):
    monkeypatch.setattr(life_story, "MIN_ITEMS", 500)
    assert _life(lived) is None


def test_nothing_hidden_counts(lived):
    conn, ids = lived["conn"], lived["ids"]
    # Thatha is only ever in hidden photographs now: never named, never new.
    conn.executemany("UPDATE assets SET visibility=? WHERE id=?",
                     [(auth.VIS_HIDDEN, i) for i in ids[:3]])
    conn.commit()
    got = _life(lived, names={lived["thatha"]: ""})
    assert got["person"]["photos"] == len(ids) - 3
    assert got["chapters"][0]["start"] == 1980
    assert all(p["id"] not in ids[:3] for c in got["chapters"] for p in c["photos"])
    assert all(p["name"] != "Thatha" for c in got["chapters"] for p in c["with"] + c["new"])


def test_the_api_names_only_who_the_viewer_may_see(as_family, lived):
    conn, ids = lived["conn"], lived["ids"]
    conn.executemany("UPDATE assets SET visibility=? WHERE id=?",
                     [(auth.VIS_HIDDEN, i) for i in ids[:3]])
    conn.commit()
    listed = as_family.get("/api/life").get_json()
    assert [p["name"] for p in listed["people"]] == ["Paati", "Amma"]
    assert listed["writer"] is False
    assert as_family.get(f"/api/life/{lived['thatha']}").status_code == 404
    got = as_family.get(f"/api/life/{lived['paati']}").get_json()
    assert got["written"] is None
    named = json.dumps(got["chapters"])
    assert "Thatha" not in named and "Amma" in named
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["life"] is True


def test_a_guest_has_no_life_story(as_guest, lived):
    assert as_guest.get("/api/life").status_code in (401, 403, 404)
    assert as_guest.get(f"/api/life/{lived['paati']}").status_code in (401, 403, 404)


def test_stories_their_own_voice_first(as_family, lived):
    ids = lived["ids"]
    for who, text in (("Amma", "Amma remembers the wedding"), ("Paati", "My wedding day in Madurai")):
        told = as_family.post(f"/api/asset/{ids[0]}/stories",
                              data={"audio": (io.BytesIO(WEBM), "s.webm", "audio/webm"),
                                    "text": text, "speaker": who},
                              content_type="multipart/form-data")
        assert told.status_code == 201
    first = _life(lived)["chapters"][0]
    assert first["stories_count"] == 2
    assert first["stories"][0]["text"].startswith("My wedding day")
    assert first["stories"][0]["id"]


def test_the_year_born_is_kept(as_family, lived):
    assert as_family.put(f"/api/life/{lived['thatha']}/born", json={"year": 1945}).status_code == 200
    assert born_store.born(lived["conn"])[lived["thatha"]] == 1945
    assert as_family.put(f"/api/life/{lived['thatha']}/born", json={"year": "x"}).status_code == 400
    assert as_family.put(f"/api/life/{lived['thatha']}/born", json={"year": None}).status_code == 200
    assert lived["thatha"] not in born_store.born(lived["conn"])


def test_without_the_writing_model_the_page_writes_its_own(as_family, lived):
    got = as_family.post(f"/api/life/{lived['paati']}/words", json={"language": "ta"})
    assert got.status_code == 503 and got.get_json()["error"]


class FakeHost:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def call(self, capability, **kwargs):
        assert capability == "generate"
        self.calls.append(kwargs)
        return {"text": self.text, "model": "text-model"}


ANSWER = ("```json\n" + json.dumps({"opening": "A life in **three** parts.", "chapters": [
    {"title": "Madurai, with Thatha", "text": "Paati at twenty-five in Madurai."},
    {"title": "Amma", "text": "Amma arrives."},
    {"title": "Grandchildren", "text": "The later years."}]}) + "\n```")


def test_the_words_are_written_once_and_kept(as_family, lived, monkeypatch):
    host = FakeHost(ANSWER)
    monkeypatch.setattr(api_life_story, "_writer", lambda: (True, ""))
    monkeypatch.setattr(api_life_story, "_host", lambda: host)
    url = f"/api/life/{lived['paati']}"
    first = as_family.post(f"{url}/words", json={"language": "ta"}).get_json()
    assert first["opening"] == "A life in three parts."
    assert [c["title"] for c in first["chapters"]] == ["Madurai, with Thatha", "Amma", "Grandchildren"]
    again = as_family.post(f"{url}/words", json={"language": "ta"}).get_json()
    assert again["chapters"] == first["chapters"] and len(host.calls) == 1
    assert as_family.get(f"{url}?language=ta").get_json()["written"]["opening"] == first["opening"]
    assert as_family.get(f"{url}?language=en").get_json()["written"] is None
    system, user = host.calls[0]["messages"]
    assert "Tamil" in system["content"] and "ONLY the facts" in system["content"]
    facts = json.loads(user["content"].split("\n", 1)[1])
    assert facts["name"] == "Paati" and facts["born"] == 1950
    assert facts["chapters"][0]["first_photographed_together"] == ["Thatha (husband or wife)"]
    assert facts["chapters"][0]["born_this_decade"] == ["Amma (child, 1978)"]
    assert "rel_path" not in user["content"] and lived["cfg"].active_root not in user["content"]


def test_words_follow_the_facts(as_family, lived, monkeypatch):
    """Another viewer, or the same one after a photograph is hidden, has other
    facts: the words kept for the old ones are not shown."""
    host = FakeHost(ANSWER)
    monkeypatch.setattr(api_life_story, "_writer", lambda: (True, ""))
    monkeypatch.setattr(api_life_story, "_host", lambda: host)
    url = f"/api/life/{lived['paati']}"
    assert as_family.post(f"{url}/words", json={"language": "en"}).status_code == 200
    lived["conn"].executemany("UPDATE assets SET visibility=? WHERE id=?",
                              [(auth.VIS_HIDDEN, i) for i in lived["ids"][:3]])
    lived["conn"].commit()
    found = _life(lived, names={lived["thatha"]: ""})
    assert words_store.get(lived["conn"], lived["paati"], "en",
                           life_story.fingerprint(found, "en")) is None


def test_a_reply_in_the_wrong_shape_is_not_kept(as_family, lived, monkeypatch):
    host = FakeHost("Paati had a lovely life.")
    monkeypatch.setattr(api_life_story, "_writer", lambda: (True, ""))
    monkeypatch.setattr(api_life_story, "_host", lambda: host)
    got = as_family.post(f"/api/life/{lived['paati']}/words", json={"language": "en"})
    assert got.status_code == 503


def test_parsed_needs_one_entry_per_chapter():
    good = json.dumps({"opening": "x", "chapters": [{"title": "a", "text": "b"}]})
    assert life_story.parsed(good, 1)["chapters"] == [{"title": "a", "text": "b"}]
    assert life_story.parsed(good, 2) is None
    assert life_story.parsed("no json here", 1) is None
    assert life_story.parsed('{"chapters": "x"}', 1) is None


def test_relations_from_the_tree():
    links = [{"person_a": 1, "person_b": 2, "kind": "parent"},
             {"person_a": 2, "person_b": 3, "kind": "parent"},
             {"person_a": 1, "person_b": 4, "kind": "parent"},
             {"person_a": 1, "person_b": 5, "kind": "spouse"}]
    rel = life_story._relation                                        # noqa: SLF001
    assert rel(2, 1, links) == "parent" and rel(1, 2, links) == "child"
    assert rel(3, 1, links) == "grandparent" and rel(1, 3, links) == "grandchild"
    assert rel(2, 4, links) == "sibling" and rel(5, 1, links) == "spouse"
    assert rel(3, 5, links) == ""
