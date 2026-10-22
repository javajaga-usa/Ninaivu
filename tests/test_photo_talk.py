"""Talk about this photo: a question about the open photograph, answered by
the writing model looking at it.

Off until switched on; for the family only; a hidden or explicit photograph
is never handed to the model, even by the administrator who can see it; the
picture goes to the model host as a small upright JPEG that is removed when
the answer is back, with only what the asker may see in the viewer beside
it; the model's vision part is pinned with the writing model and loaded
beside it, not as a second copy.
"""
from __future__ import annotations

import base64
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from ninaivu import runtime
from ninaivu.api import api_photo_talk
from ninaivu.media import model_catalog, photo_talk
from ninaivu.runtime import CAPABILITIES, Unavailable, host, llm
from ninaivu.server import auth
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db


# --- the switch -------------------------------------------------------------------

def test_the_switch_is_off_and_lives_in_advanced():
    assert Config().photo_talk is False
    assert "photo_talk" in GROUPS["Advanced"]


def _picture(conn) -> int:
    return conn.execute("SELECT id FROM assets WHERE kind='picture' AND live_clip=0 "
                        "AND raw_twin=0 AND trashed=0 ORDER BY id").fetchone()[0]


@pytest.fixture()
def talking(app, people):
    cfg = app.config["MV_CONFIG"]
    cfg.photo_talk = True
    conn = people["conn"]
    aid = _picture(conn)
    with db._write_lock:                                              # noqa: SLF001
        conn.execute("UPDATE assets SET date_key='1998-01-14', date_source='exif', "
                     "city='Madurai', country='India', visibility=1, nsfw=0 WHERE id=?", (aid,))
        paati = conn.execute("INSERT INTO people_clusters(name, created_at) "
                             "VALUES ('Paati', 0)").lastrowid
        appa = conn.execute("INSERT INTO people_clusters(name, created_at) "
                            "VALUES ('Appa', 0)").lastrowid
        for pid, x in ((paati, 60), (appa, 5)):
            conn.execute("INSERT INTO faces(asset_id, person_id, source, bbox, embedding, quality) "
                         "VALUES (?, ?, 'confirmed', ?, x'00', 0.9)", (aid, pid, f"[{x},0,10,10]"))
        conn.commit()
    return {"id": aid, "conn": conn, "cfg": cfg}


def test_off_it_is_not_there(as_family, app, people):
    aid = _picture(people["conn"])
    assert as_family.get(f"/api/asset/{aid}/talk").status_code == 404
    assert as_family.post(f"/api/asset/{aid}/talk", json={"question": "Who?"}).status_code == 404


def test_the_status_says_it_is_on(as_family, talking):
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["talk"] is True


def test_a_guest_is_not_offered_it(as_guest, talking):
    with db._write_lock:                                              # noqa: SLF001
        talking["conn"].execute("UPDATE assets SET visibility=0 WHERE id=?", (talking["id"],))
        talking["conn"].commit()
    assert as_guest.get(f"/api/asset/{talking['id']}/talk").status_code in (403, 404)


def test_without_the_engine_it_says_why(as_family, talking):
    got = as_family.get(f"/api/asset/{talking['id']}/talk").get_json()
    assert got["allowed"] is True and got["ready"] is False
    assert "AI engine is off" in got["why"]
    asked = as_family.post(f"/api/asset/{talking['id']}/talk", json={"question": "Where is this?"})
    assert asked.status_code == 409


class FakeHost:
    def __init__(self, text="It is the **Meenakshi** temple's east tower.", error=None):
        self.text, self.error = text, error
        self.calls: list[dict] = []

    def call(self, capability, **kwargs):
        assert capability == "look"
        path = Path(kwargs["path"])
        with Image.open(path) as shown:
            kwargs["shown"] = (shown.format, shown.size)
        kwargs["mode"] = path.stat().st_mode & 0o777
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"text": self.text, "model": "text-model", "took": 1.5}


@pytest.fixture()
def host_here(monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(api_photo_talk, "_ready", lambda: None)
    monkeypatch.setattr(api_photo_talk, "_host", lambda: fake)
    return fake


def test_a_question_is_answered_from_the_photograph(as_family, talking, host_here):
    got = as_family.post(f"/api/asset/{talking['id']}/talk",
                         json={"question": "Which temple is this?"})
    assert got.status_code == 200
    answer = got.get_json()
    assert answer["answer"] == "It is the Meenakshi temple's east tower."
    assert answer["language"] == "en" and answer["model"] == "text-model"
    (call,) = host_here.calls
    assert call["shown"][0] == "JPEG" and max(call["shown"][1]) <= api_photo_talk.MAX_SIDE
    assert call["mode"] == 0o600
    assert not Path(call["path"]).exists(), "the picture handed over is removed"
    system, question = call["messages"]
    assert question == {"role": "user", "content": "Which temple is this?"}
    assert "Taken: 1998-01-14" in system["content"] and "Place: Madurai, India" in system["content"]
    assert "left to right: Appa, Paati" in system["content"]
    assert "Never give anybody a name the notes do not give" in system["content"]
    assert talking["cfg"].active_root not in system["content"]


def test_a_tamil_question_is_answered_in_tamil_with_the_talk_so_far(as_family, talking, host_here):
    turns = [{"q": "Where is this?", "a": "Madurai."}, {"q": "", "a": "dropped"}, "junk"]
    got = as_family.post(f"/api/asset/{talking['id']}/talk",
                         json={"question": "பலகையில் என்ன எழுதியிருக்கிறது?", "turns": turns})
    assert got.get_json()["language"] == "ta"
    messages = host_here.calls[0]["messages"]
    assert "Tamil, in Tamil script" in messages[0]["content"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "Where is this?" and messages[2]["content"] == "Madurai."


def test_a_file_date_is_not_a_date_taken(as_family, talking, host_here):
    with db._write_lock:                                              # noqa: SLF001
        talking["conn"].execute("UPDATE assets SET date_source='mtime' WHERE id=?", (talking["id"],))
        talking["conn"].commit()
    as_family.post(f"/api/asset/{talking['id']}/talk", json={"question": "When?"})
    assert "Taken:" not in host_here.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("column, value, words", [("visibility", auth.VIS_HIDDEN, "hidden"),
                                                  ("nsfw", 1, "explicit")])
def test_hidden_and_explicit_are_never_shown_to_the_model(as_admin, talking, host_here,
                                                          column, value, words):
    with db._write_lock:                                              # noqa: SLF001
        talking["conn"].execute(f"UPDATE assets SET {column}=? WHERE id=?", (value, talking["id"]))
        talking["conn"].commit()
    status = as_admin.get(f"/api/asset/{talking['id']}/talk").get_json()
    assert status["allowed"] is False and words in status["kept"]
    got = as_admin.post(f"/api/asset/{talking['id']}/talk", json={"question": "What is this?"})
    assert got.status_code == 403 and words in got.get_json()["error"]
    assert host_here.calls == []


def test_a_family_member_cannot_ask_about_a_hidden_photograph(as_family, talking, host_here):
    with db._write_lock:                                              # noqa: SLF001
        talking["conn"].execute("UPDATE assets SET visibility=? WHERE id=?",
                                (auth.VIS_HIDDEN, talking["id"]))
        talking["conn"].commit()
    assert as_family.post(f"/api/asset/{talking['id']}/talk",
                          json={"question": "What is this?"}).status_code == 404
    assert host_here.calls == []


@pytest.mark.parametrize("question", ["", "   ", "x" * (photo_talk.MAX_QUESTION + 1)])
def test_a_question_must_be_there_and_short(as_family, talking, host_here, question):
    got = as_family.post(f"/api/asset/{talking['id']}/talk", json={"question": question})
    assert got.status_code == 400 and host_here.calls == []


def test_a_busy_host_says_so_and_leaves_nothing_behind(as_family, talking, host_here):
    host_here.error = runtime.HostBusy("transcribe")
    got = as_family.post(f"/api/asset/{talking['id']}/talk", json={"question": "Who?"})
    assert got.status_code == 429
    assert not Path(host_here.calls[0]["path"]).exists()


def test_notes_and_answers_are_plain():
    faces = [{"person_name": "Paati", "bbox": "[50,0,1,1]"}, {"person_name": None, "bbox": "[0,0,1,1]"},
             {"person_name": "Amma", "bbox": [10, 0, 1, 1]}, {"person_name": "Paati", "bbox": "[90,0,1,1]"}]
    assert photo_talk.names_in(faces) == ["Amma", "Paati"]
    row = {"date_key": "2001-04-14", "date_source": "filename", "city": None, "country": "India",
           "caption": "Puthandu", "caption_source": "manual"}
    lines = photo_talk.notes(row, names=["Amma"], description="Two women by a kolam.")
    assert lines == ["Taken: 2001-04-14", "Place: India",
                     "People in it, as the family named them, left to right: Amma",
                     "What the photo engine saw in it: Two women by a kolam.",
                     "The family's caption: Puthandu"]
    assert photo_talk.notes({**row, "caption_source": "auto"}, names=[], description=None)[-1] \
        == "Place: India"
    assert photo_talk.cleaned("**Yes.**\n- A kolam\n\n\n\n# Done") == "Yes.\nA kolam\n\nDone"


# --- the model's half -------------------------------------------------------------

def test_the_vision_part_is_pinned_with_the_writing_model():
    writer = model_catalog.MODELS["text-model"]["files"][0]
    (part,) = model_catalog.MODELS["vision-part"]["files"]
    assert part["repo"] == writer["repo"] and part["revision"] == writer["revision"]
    assert part["folder"] == writer["folder"] and part["name"] == "mmproj-model-f16-12B.gguf"
    assert part["bytes"] == 854200864 and len(part["sha256"]) == 64
    assert model_catalog.MODELS["vision-part"]["requires"] == ["llama_cpp"]
    assert llm.VISION_PARTS["text-model"] == "vision-part"


def test_the_capability_is_called_look():
    assert CAPABILITIES["look"] == "ninaivu.runtime.llm:LOOK"
    assert host.resolve(CAPABILITIES["look"]) is llm.LOOK


class FakeLlama:
    made: list[dict] = []

    def __init__(self, **kwargs):
        FakeLlama.made.append(kwargs)
        self.chat_handler = None
        self.asked: list[dict] = []
        self.resets = 0

    def create_chat_completion(self, **kwargs):
        self.asked.append({**kwargs, "handler": self.chat_handler})
        return {"choices": [{"message": {"content": "  A temple tower.  "}}],
                "usage": {"completion_tokens": 5}}

    def reset(self):
        self.resets += 1


class FakeEyes:
    def __init__(self, clip_model_path, verbose):
        self.path = clip_model_path


@pytest.fixture()
def runner(monkeypatch, tmp_path):
    module = types.ModuleType("llama_cpp")
    module.Llama = FakeLlama
    module.__spec__ = types.SimpleNamespace(name="llama_cpp")
    chat = types.ModuleType("llama_cpp.llama_chat_format")
    chat.MTMDChatHandler = FakeEyes
    monkeypatch.setitem(sys.modules, "llama_cpp", module)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_chat_format", chat)
    monkeypatch.setattr(llm, "missing_package", lambda: None)
    monkeypatch.setattr(llm, "_kept", None)
    FakeLlama.made.clear()
    model_catalog.configure(tmp_path)
    paths = []
    for model_id in ("text-model", "vision-part"):
        entry = model_catalog.MODELS[model_id]["files"][0]
        # Small stand-ins: never a file of the real size (see test_language_model).
        monkeypatch.setitem(entry, "bytes", 4096)
        path = model_catalog.file_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 4096)
        paths.append(path)
    yield paths
    model_catalog.configure(None)


def test_without_the_vision_part_nothing_is_looked_at(runner):
    runner[1].unlink()
    assert "vision part" in llm.vision_ready()
    with pytest.raises(Unavailable):
        llm._load_eyes({})


def test_looking_shares_the_loaded_model_and_hands_it_the_picture(runner, tmp_path):
    writer = llm._load({"threads": 3})
    eyes = llm._load_eyes({"threads": 3})
    assert eyes is writer and len(FakeLlama.made) == 1, "one model in memory for both"
    assert writer.eyes.path == str(runner[1])
    picture = tmp_path / "shown.jpg"
    Image.new("RGB", (40, 30), "orange").save(picture, "JPEG")
    answer = llm.look(writer, path=str(picture), messages=[
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Which temple?"},
        {"role": "assistant", "content": "Madurai."},
        {"role": "user", "content": "Which tower?"}], max_tokens=99999)
    assert answer == {"text": "A temple tower.", "model": "text-model", "tokens": 5,
                      "took": answer["took"]}
    asked = writer.llama.asked[-1]
    assert asked["handler"] is writer.eyes and asked["max_tokens"] == llm.MAX_TOKENS
    first = asked["messages"][1]["content"]
    url = first[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == picture.read_bytes()
    assert first[1] == {"type": "text", "text": "Which temple?"}
    assert asked["messages"][3] == {"role": "user", "content": "Which tower?"}
    assert writer.llama.chat_handler is None and writer.llama.resets == 1, \
        "writing goes on without the picture's reader"


def test_a_missing_or_huge_picture_is_refused(runner, tmp_path, monkeypatch):
    writer = llm._load_eyes({})
    with pytest.raises(ValueError):
        llm.look(writer, path=str(tmp_path / "gone.jpg"), messages=[{"role": "user", "content": "?"}])
    big = tmp_path / "big.jpg"
    big.write_bytes(b"\0" * 64)
    monkeypatch.setattr(llm, "MAX_PICTURE_BYTES", 10)
    with pytest.raises(ValueError):
        llm.look(writer, path=str(big), messages=[{"role": "user", "content": "?"}])
