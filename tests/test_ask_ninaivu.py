"""Ask Ninaivu: questions about the family's photographs in Tamil or English,
answered on this computer from what the library knows, inside the asker's own
limits; plainly without a model, in words with the writing model."""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.api import api_ask_ninaivu
from ninaivu.ask import answer, question
from ninaivu.media.scanner import Scanner
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, journal

PEOPLE = [(1, "Paati"), (2, "Arjun Kumar"), (3, "பாட்டி"), (4, "அருண்")]
PLACES = ["Ooty", "Chennai", "ஊட்டி"]
TODAY = date(2026, 10, 19)


def read(text: str) -> question.Question:
    return question.understand(text, people=PEOPLE, places=PLACES, today=TODAY)


# --- reading a question ---------------------------------------------------------

def test_off_by_default_and_with_the_family_settings():
    assert Config().ask_ninaivu is False
    assert "ask_ninaivu" in GROUPS["People"]


@pytest.mark.parametrize("text, intent", [
    ("How many photos of Arjun from 2019?", "count"),
    ("When did we last go to Ooty?", "last"),
    ("When was the first time we went to Chennai?", "first"),
    ("When was Diwali at home?", "when"),
    ("Where did we go last year?", "where"),
    ("Who was at the beach?", "who"),
    ("What's new this week?", "new"),
    ("show me birthday cake photos", "show"),
    ("பாட்டியுடன் கடைசியாக ஊட்டிக்கு எப்போது போனோம்?", "last"),
    ("அருண் 2019ல் எத்தனை படங்கள்?", "count"),
    ("யார் கல்யாணத்தில் இருந்தார்கள்?", "who"),
    ("எங்கே போனோம்?", "where"),
])
def test_what_is_asked(text, intent):
    assert read(text).intent == intent


def test_an_english_question_is_read_like_the_search_box():
    q = read("When did we last go to Ooty with Paati?")
    assert (q.language, q.people, q.place, q.text) == ("en", [(1, "Paati")], "Ooty", "")
    q = read("How many photos of Arjun from 2019?")
    assert q.people == [(2, "Arjun Kumar")]
    assert (q.window.date_from, q.window.date_to) == ("2019-01-01", "2019-12-31")
    assert read("Who was at the beach in December 2020?").text == "beach"
    assert read("show me birthday cake photos").text == "birthday cake"


def test_a_tamil_question_with_tamil_endings():
    q = read("பாட்டியுடன் கடைசியாக ஊட்டிக்கு எப்போது போனோம்?")
    assert (q.language, q.people, q.place, q.text) == ("ta", [(3, "பாட்டி")], "ஊட்டி", "")
    q = read("அருண் 2019ல் எத்தனை படங்கள்?")
    assert q.people == [(4, "அருண்")] and q.window.date_from == "2019-01-01" and q.text == ""
    q = read("போன வருடம் கடற்கரை படங்கள் காட்டு")
    assert q.window.date_from == "2025-01-01" and q.text == "கடற்கரை"
    assert read("டிசம்பர் 2020 தீபாவளி").window.date_from == "2020-12-01"


def test_tamil_words_that_only_start_like_a_month_or_hold_added():
    """D-12: மே (May) is a whole word or has a date ending; சேர்த்து is
    "together", not "added"."""
    people = PEOPLE + [(5, "மேரி"), (6, "தாத்தா")]
    q = question.understand("மேரி படங்கள் காட்டு", people=people, places=["மேகமலை"], today=TODAY)
    assert q.people == [(5, "மேரி")] and q.window is None
    q = question.understand("மேகமலை படங்கள்", people=people, places=["மேகமலை"], today=TODAY)
    assert q.place == "மேகமலை" and q.window is None
    assert read("மேடை நிகழ்ச்சி").window is None and read("மேடை நிகழ்ச்சி").text == "மேடை நிகழ்ச்சி"
    assert read("மேயில் எடுத்த படங்கள்").window.date_from == "2026-05-01"
    assert read("மே 2020 படங்கள்").window.date_from == "2020-05-01"
    q = question.understand("பாட்டி தாத்தா சேர்த்து படங்கள்", people=people, places=PLACES,
                            today=TODAY)
    assert q.intent == "show" and q.people == [(3, "பாட்டி"), (6, "தாத்தா")] and q.text == ""
    assert read("புதிதாக சேர்த்த படங்கள்").intent == "new"


def test_a_name_is_only_one_the_viewer_can_see():
    q = question.understand("photos of Paati", people=[], places=[], today=TODAY)
    assert q.people == [] and q.text == "Paati"


def test_the_models_reading_is_held_to_known_names_places_and_real_dates():
    q = read("பழைய கோயில் திருவிழா")
    plan = {"intent": "show", "search": "temple festival", "people": ["Paati", "Somebody Else"],
            "place": "Atlantis", "from": "2019-04-01", "to": "2019-04-30"}
    merged = question.merge_plan(q, plan, people=PEOPLE, places=PLACES)
    assert merged.people == [(1, "Paati")] and merged.place == ""
    assert merged.text == "temple festival" and merged.planned
    assert (merged.window.date_from, merged.window.date_to) == ("2019-04-01", "2019-04-30")
    # Nonsense changes nothing.
    again = read("Paati in Ooty")
    assert question.merge_plan(again, "not a plan").people == [(1, "Paati")]
    bad = question.merge_plan(read("x"), {"from": "2020-05-01", "to": "2019-01-01"})
    assert bad.window is None
    # An English question keeps its own words.
    english = question.merge_plan(read("show me the old temple"), plan, people=PEOPLE, places=PLACES)
    assert english.text == "old temple"


def test_the_models_citations_are_checked():
    text, cited = answer.checked("Here [#5] and there [#9]. And [#5] again [#77].", [5, 9])
    assert text == "Here [#5] and there [#9]. And [#5] again." and cited == [5, 9]


# --- answering --------------------------------------------------------------------

@pytest.fixture()
def family_album(app, people):
    """Paati and Arjun in the 2023 photographs; some in Ooty, one in Chennai."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    cfg.ask_ninaivu = True
    shots = conn.execute("SELECT id, rel_path FROM assets WHERE rel_path LIKE '2023/%' "
                         "AND rel_path NOT LIKE '%copy%' ORDER BY captured_at").fetchall()
    ids = [r["id"] for r in shots]
    with db._write_lock:
        paati = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Paati', 0)").lastrowid
        arjun = conn.execute("INSERT INTO people_clusters(name, created_at) VALUES ('Arjun', 0)").lastrowid
        for aid in ids[:4]:
            conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                         "VALUES (?, ?, 'confirmed', '[0,0,1,1]', x'00')", (aid, paati))
        for aid in ids[2:5]:
            conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding) "
                         "VALUES (?, ?, 'confirmed', '[0,0,1,1]', x'00')", (aid, arjun))
        conn.executemany("UPDATE assets SET city='Ooty', country='India' WHERE id=?",
                         [(i,) for i in ids[:3]])
        conn.execute("UPDATE assets SET city='Chennai', country='India' WHERE id=?", (ids[4],))
        conn.execute("INSERT INTO descriptions(asset_id, text, model, version, made_at) "
                     "VALUES (?, 'An old temple lit with oil lamps at a festival', 'test', 1, 0)",
                     (ids[5],))
        conn.commit()
    return {"ids": ids, "paati": paati, "arjun": arjun, "conn": conn, "cfg": cfg}


def ask(client, text, **extra):
    return client.post("/api/ask-ninaivu", json={"question": text, **extra})


def test_off_it_is_not_there(as_family, app):
    assert as_family.get("/api/ask-ninaivu").status_code == 404
    assert ask(as_family, "how many photos?").status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["ask"] is False


def test_guests_are_not_answered(as_guest, family_album):
    assert ask(as_guest, "Where was Paati?").status_code in (403, 404)


def test_the_status_says_whether_the_model_writes(as_family, family_album):
    got = as_family.get("/api/ask-ninaivu").get_json()
    assert got["on"] is True and got["writer"] is False and "engine is off" in got["why"]
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["ask"] is True


def test_when_did_we_last_go_to_ooty_with_paati(as_family, family_album):
    ids = family_album["ids"]
    got = ask(as_family, "When did we last go to Ooty with Paati?").get_json()
    assert got["intent"] == "last" and got["total"] == 3
    assert {c["type"] for c in got["understood"]} == {"person", "place"}
    assert ids[2] == got["items"][0]["id"], "the most recent first"
    keys = [line["key"] for line in got["lines"]]
    assert keys == ["I found {n} photos and videos.", "The most recent is from {date}, in {place}."]
    last = db.get_asset(family_album["conn"], ids[2])
    assert got["lines"][1]["params"] == {
        "date": time.strftime("%Y-%m-%d", time.localtime(last["captured_at"])), "place": "Ooty"}
    assert got["written"] == "" and got["model"] == ""


def test_how_many_photos_and_who_else_is_in_them(as_family, family_album):
    got = ask(as_family, "How many photos of Arjun?").get_json()
    assert got["total"] == 3
    lines = {line["key"]: line["params"] for line in got["lines"]}
    assert lines["With {people}."] == {"people": "Paati"}
    assert "Ooty" in lines["Mostly in {places}."]["places"]


def test_the_words_of_a_description_are_searched(as_family, family_album):
    got = ask(as_family, "show me the temple").get_json()
    assert [i["id"] for i in got["items"]] == [family_album["ids"][5]]


def test_nothing_found_says_so(as_family, family_album):
    got = ask(as_family, "How many photos from 1999?").get_json()
    assert got["total"] == 0
    assert got["lines"] == [{"key": "I could not find any photos for that.", "params": {}}]


def test_an_answer_never_reaches_past_what_the_asker_may_see(as_family, as_admin, family_album):
    conn = family_album["conn"]
    hidden = family_album["ids"][0]
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden,))
        conn.commit()
    family = ask(as_family, "How many photos of Paati?").get_json()
    assert family["total"] == 3 and hidden not in [i["id"] for i in family["items"]]
    admin = ask(as_admin, "How many photos of Paati?").get_json()
    assert admin["total"] == 4 and hidden in [i["id"] for i in admin["items"]]


def test_the_model_is_never_told_about_a_hidden_photograph(family_album, app):
    conn = family_album["conn"]
    hidden = family_album["ids"][1]
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility=2, ocr_text='bank account 1234' WHERE id=?",
                     (hidden,))
        conn.commit()
    rows = [dict(db.get_asset(conn, i)) for i in family_album["ids"]]
    told = answer.moments(conn, rows, {family_album["paati"]: "Paati"})
    assert hidden not in [m["id"] for m in told]
    assert "bank account" not in json.dumps(told)
    assert any(m.get("people") == ["Paati"] for m in told)
    assert any("temple" in m.get("description", "") for m in told)


def test_an_admins_question_tells_the_model_nothing_of_hidden_or_private_photos(
        as_admin, family_album, monkeypatch):
    """D-05: the places, dates and people handed to the model are of only the
    photographs it may be told of, though an administrator's count is of all."""
    conn, ids = family_album["conn"], family_album["ids"]
    with db._write_lock:
        conn.execute("UPDATE assets SET visibility=2, city='Kodaikanal', captured_at=? "
                     "WHERE id=?", (time.mktime((2011, 6, 1, 12, 0, 0, 0, 0, -1)), ids[0]))
        conn.execute("UPDATE assets SET private_to=1, city='Madurai' WHERE id=?", (ids[1],))
        conn.commit()
    host = FakeHost("Here they are.")
    monkeypatch.setattr(api_ask_ninaivu, "_writer", lambda: (True, ""))
    monkeypatch.setattr(api_ask_ninaivu, "_host", lambda: host)
    got = ask(as_admin, "How many photos of Paati?").get_json()
    assert got["total"] == 4
    told = host.calls[-1]["messages"][1]["content"]
    facts = json.loads(told.split("Facts (JSON):\n", 1)[1])
    assert facts["matching_items"] == 4
    assert "Kodaikanal" not in told and "Madurai" not in told
    assert "2011" not in facts["items_per_year"] and not facts["first_date"].startswith("2011")
    assert [p["items"] for p in facts["people_in_them"] if p["name"] == "Paati"] == [2]
    # The administrator's own plain answer still counts them all.
    assert got["lines"]


class FakeHost:
    """The model host, with a writing model that answers as told."""

    def __init__(self, answer_text: str):
        self.answer_text = answer_text
        self.calls: list[dict] = []

    def call(self, capability, **kwargs):
        assert capability == "generate"
        self.calls.append(kwargs)
        if kwargs.get("json_schema"):
            return {"text": json.dumps({"intent": "last", "search": "", "people": ["Paati"],
                                        "place": "Ooty", "from": "", "to": ""}),
                    "model": "text-model"}
        return {"text": self.answer_text, "model": "text-model"}


def test_with_the_writing_model_the_answer_is_in_words(as_family, family_album, monkeypatch):
    ids = family_album["ids"]
    host = FakeHost(f"பாட்டியுடன் ஊட்டிக்கு கடைசியாக சென்றது [#{ids[2]}] [#999999].")
    monkeypatch.setattr(api_ask_ninaivu, "_writer", lambda: (True, ""))
    monkeypatch.setattr(api_ask_ninaivu, "_host", lambda: host)
    got = ask(as_family, "பாட்டியுடன் கடைசியாக ஊட்டிக்கு எப்போது போனோம்?").get_json()
    assert got["language"] == "ta" and got["model"] == "text-model"
    assert got["written"] == f"பாட்டியுடன் ஊட்டிக்கு கடைசியாக சென்றது [#{ids[2]}]."
    assert got["cited"] == [ids[2]] and got["items"][0]["id"] == ids[2]
    assert got["total"] == 3, "the model read Paati and Ooty where the rules could not"
    # The answer is asked for in Tamil, from the facts only.
    system = host.calls[-1]["messages"][0]["content"]
    assert "Tamil" in system and "ONLY the facts" in system
    # Asked to answer plainly, the model is not used.
    host.calls.clear()
    assert ask(as_family, "How many photos of Paati?", plain=True).get_json()["written"] == ""
    assert host.calls == []


def test_a_model_that_fails_leaves_the_plain_answer(as_family, family_album, monkeypatch):
    class Broken:
        def call(self, *_a, **_k):
            raise RuntimeError("out of memory")
    monkeypatch.setattr(api_ask_ninaivu, "_writer", lambda: (True, ""))
    monkeypatch.setattr(api_ask_ninaivu, "_host", lambda: Broken())
    got = ask(as_family, "How many photos of Paati?").get_json()
    assert got["written"] == "" and got["total"] == 4 and got["lines"]


def test_what_is_new_comes_from_the_change_journal(as_family, family_album):
    cfg, conn = family_album["cfg"], family_album["conn"]
    cfg.change_journal = True
    journal.switch(conn, True)
    Image.new("RGB", (320, 200), (5, 5, 5)).save(Path(cfg.active_root) / "misc/arrived.jpg")
    Scanner(cfg)._run(Path(cfg.active_root), full=False)
    got = ask(as_family, "What's new this week?").get_json()
    assert got["intent"] == "new" and got["total"] == 1
    assert got["items"][0]["name"] == "arrived.jpg"
    assert got["lines"][0]["key"] == "{n} new photos and videos have been added since {date}."


def test_a_question_must_be_there_and_not_too_long(as_family, family_album):
    assert ask(as_family, "").status_code == 400
    assert ask(as_family, "x" * 600).status_code == 400
