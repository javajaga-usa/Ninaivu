"""Easy view: for a grandparent, a calmer and larger family app, per profile,
with each photograph told aloud from what the index already knows."""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pytest

from ninaivu.features.easy_view import media as told
from ninaivu.features.easy_view import routes as api_easy
from ninaivu.features.easy_view import storage as easy_store
from ninaivu.runtime import voice as voice_mod
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db

TODAY = dt.date(2026, 11, 14)


# --- the sentences -------------------------------------------------------------------

def test_a_photograph_is_told_who_where_and_when_in_english():
    row = {"kind": "picture", "date_key": "1985-01-10", "date_source": "exif",
           "city": "Madurai", "country": "India"}
    assert told.sentences(row, names=["Kamala", "Raman"], lang="en", today=TODAY) == [
        "In this photograph: Kamala and Raman.",
        "Taken in Madurai, India.",
        "On Thursday, 10 January 1985.",
        "That was 41 years ago.",
    ]


def test_and_in_tamil():
    row = {"kind": "video", "date_key": "2025-11-14", "date_source": "exif", "city": "Chennai"}
    assert told.sentences(row, names=["கமலா"], lang="ta", today=TODAY) == [
        "இந்த காணொளியில்: கமலா.",
        "இடம்: Chennai.",
        "நாள்: 14 நவம்பர் 2025, வெள்ளிக்கிழமை.",
        "ஒரு ஆண்டுக்கு முன்பு.",
    ]


def test_a_file_clock_date_is_never_said_and_nothing_known_is_just_a_photograph():
    row = {"kind": "picture", "date_key": "2019-03-02", "date_source": "mtime"}
    assert told.sentences(row, names=[], lang="en", today=TODAY) == ["A photograph."]
    assert told.sentences(row, names=[], lang="ta", today=TODAY) == ["ஒரு படம்."]


def test_many_people_are_counted_and_only_a_family_caption_is_read():
    row = {"kind": "picture", "caption": "Paati's 80th birthday", "caption_source": "manual"}
    said = told.sentences(row, names=["A", "B", "C", "D", "E", "F"], lang="en", today=TODAY)
    assert said == ["In this photograph: A, B, C, D and 2 others.",
                    "The family wrote: Paati's 80th birthday."]
    row["caption_source"] = "ai"
    assert told.sentences(row, names=[], lang="en", today=TODAY) == ["A photograph."]


def test_the_language_falls_back_to_tamil_and_the_key_follows_the_words():
    assert told.language("en-GB") == "en" and told.language("fr") == "ta" and told.language(None) == "ta"
    assert told.key(["a"], "ta") != told.key(["a"], "en") != told.key(["b"], "en")


# --- the switch and the profiles -----------------------------------------------------

def test_off_by_default_with_the_advanced_settings():
    assert Config().easy_view is False
    assert "easy_view" in GROUPS["Advanced"]


def _a_photograph(client) -> int:
    items = client.get("/api/assets?limit=50").get_json()["items"]
    return next(i["id"] for i in items if i["kind"] == "picture")


def test_off_every_route_is_not_there(as_admin, people):
    target = _a_photograph(as_admin)
    assert as_admin.get("/api/easy-view").status_code == 404
    assert as_admin.put("/api/easy-view", json={"on": True}).status_code == 404
    assert as_admin.get("/api/easy-view/people").status_code == 404
    assert as_admin.put(f"/api/easy-view/people/{people['family'].id}", json={"on": True}).status_code == 404
    assert as_admin.get(f"/api/asset/{target}/told").status_code == 404
    assert as_admin.post(f"/api/asset/{target}/told/voice", json={}).status_code == 404
    assert as_admin.get("/api/status").get_json()["moments"]["easy_view"] is False


class FakeHost:
    def __init__(self):
        self.calls = []

    def call(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        Path(kwargs["out"]).write_bytes(b"RIFF" + b"\0" * 40)
        return {"duration": 1.0, "segments": [], "voices": [kwargs["language"]]}


@pytest.fixture()
def easy(app, scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.easy_view = True
    host = FakeHost()
    monkeypatch.setattr(api_easy, "_host", lambda: host)
    monkeypatch.setattr(voice_mod, "ready", lambda language: None)
    return {"cfg": cfg, "conn": conn, "host": host}


def test_a_person_turns_it_on_and_off_for_themselves(as_family, easy):
    assert as_family.get("/api/status").get_json()["moments"]["easy_view"] is True
    assert as_family.get("/api/easy-view").get_json()["on"] is False
    assert as_family.put("/api/easy-view", json={"on": True}).get_json()["on"] is True
    assert as_family.get("/api/easy-view").get_json()["on"] is True
    assert as_family.put("/api/easy-view", json={"on": "yes"}).status_code == 400
    assert as_family.put("/api/easy-view", json={"on": False}).get_json()["on"] is False


def test_an_administrator_turns_it_on_for_a_grandparent(as_admin, as_family, people, easy):
    family = people["family"].id
    assert as_admin.put(f"/api/easy-view/people/{family}", json={"on": True}).get_json() == \
        {"id": family, "on": True}
    assert as_admin.get("/api/easy-view/people").get_json() == {"on": [family]}
    assert as_family.get("/api/easy-view").get_json()["on"] is True
    assert easy_store.is_on(easy["conn"], family)
    assert as_admin.put("/api/easy-view/people/987654", json={"on": True}).status_code == 404
    # Only an administrator sets it for somebody else.
    assert as_family.put(f"/api/easy-view/people/{family}", json={"on": False}).status_code == 403
    assert as_family.get("/api/easy-view/people").status_code == 403


def test_a_guest_is_never_offered_it(as_guest, as_admin, easy):
    target = _a_photograph(as_admin)
    assert as_guest.get("/api/easy-view").status_code in (403, 404)
    assert as_guest.get(f"/api/asset/{target}/told").status_code in (403, 404)


# --- telling a photograph ------------------------------------------------------------

def _name_a_face(conn, asset_id: int, name: str, left: float) -> None:
    now = time.time()
    person = conn.execute("INSERT INTO people_clusters (name, created_at) VALUES (?, ?)",
                          (name, now)).lastrowid
    conn.execute("INSERT INTO faces (asset_id, person_id, source, bbox, embedding) VALUES (?, ?, 'confirmed', ?, x'00')",
                 (asset_id, person, json.dumps([left, 0.1, 0.1, 0.1])))
    conn.commit()


def test_a_photograph_is_told_with_the_names_left_to_right(as_family, easy):
    conn = easy["conn"]
    target = _a_photograph(as_family)
    db.update_asset(conn, target, date_key="1985-01-10", date_source="manual",
                    city="Madurai", country="India")
    _name_a_face(conn, target, "Raman", 0.6)
    _name_a_face(conn, target, "Kamala", 0.2)
    said = as_family.get(f"/api/asset/{target}/told?language=en").get_json()
    assert said["sentences"][:3] == ["In this photograph: Kamala and Raman.",
                                     "Taken in Madurai, India.", "On Thursday, 10 January 1985."]
    assert said["language"] == "en" and said["audio"] is None and said["voice"]["ready"] is True


def test_the_voice_reads_it_once_and_the_reading_is_kept(as_family, easy):
    cfg, host = easy["cfg"], easy["host"]
    target = _a_photograph(as_family)
    made = as_family.post(f"/api/asset/{target}/told/voice", json={"language": "ta"}).get_json()
    assert host.calls[0][0] == "speak" and host.calls[0][1]["language"] == "ta"
    assert [s["text"] for s in host.calls[0][1]["sentences"]] == made["sentences"]
    audio = as_family.get(made["audio"])
    assert audio.status_code == 200 and audio.mimetype == "audio/wav" and audio.data[:4] == b"RIFF"
    again = as_family.post(f"/api/asset/{target}/told/voice", json={"language": "ta"}).get_json()
    assert again["audio"] == made["audio"] and len(host.calls) == 1
    files = list((Path(cfg.state_dir) / "easy-view").glob("*.wav"))
    assert [f.name for f in files] == [f"{target}-{made['key']}.wav"]


def test_without_a_voice_the_page_is_told_to_use_the_devices_own(as_family, easy, monkeypatch):
    monkeypatch.setattr(voice_mod, "ready", lambda language: "the Tamil voice is not downloaded")
    target = _a_photograph(as_family)
    said = as_family.get(f"/api/asset/{target}/told?language=ta").get_json()
    assert said["voice"] == {"ready": False, "why": "the Tamil voice is not downloaded"}
    response = as_family.post(f"/api/asset/{target}/told/voice", json={"language": "ta"})
    assert response.status_code == 409 and easy["host"].calls == []


def test_a_photograph_the_viewer_may_not_see_is_not_told(as_family, as_admin, easy):
    target = _a_photograph(as_admin)
    db.update_asset(easy["conn"], target, nsfw=1)
    assert as_family.get(f"/api/asset/{target}/told").status_code == 404
    assert as_family.post(f"/api/asset/{target}/told/voice", json={}).status_code == 404
    assert as_admin.get(f"/api/asset/{target}/told").status_code == 200, "admins see everything"


def test_only_the_newest_readings_are_kept(easy, monkeypatch):
    cfg = easy["cfg"]
    monkeypatch.setattr(api_easy, "KEEP", 2)
    where = api_easy.folder(cfg)
    for n in range(4):
        path = where / f"{n}-k.wav"
        path.write_bytes(b"RIFF")
        stamp = 1_000_000 + n
        import os
        os.utime(path, (stamp, stamp))
    api_easy._keep_few(cfg)
    assert sorted(p.name for p in where.glob("*.wav")) == ["2-k.wav", "3-k.wav"]
