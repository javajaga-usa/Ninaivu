"""Letters read aloud: a photograph's words, read off it or typed by the family,
kept, and read aloud in a calm Tamil or English voice on this computer; the
reading kept for next time and for a memory film, never for other words."""
from __future__ import annotations

import importlib.util
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from ninaivu import runtime
from ninaivu.api import api_letters
from ninaivu.media import letters, model_catalog
from ninaivu.runtime import voice as voice_mod
from ninaivu.runtime.host import CAPABILITIES, resolve
from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS
from ninaivu.storage import db, letters as letters_store

LETTER = ("அன்புள்ள கண்ணன்,\nநாங்கள் 1962 மார்ச் 12 அன்று மதுரை வந்து சேர்ந்தோம். "
          "அம்மா உன்னை நினைத்துக்கொண்டே இருக்கிறாள்.\n\nஆசீர்வாதங்களுடன், அப்பா")


# --- the words -----------------------------------------------------------------------

def test_the_language_is_the_script_most_of_the_letters_are_in():
    assert letters.language_of(LETTER) == "ta"
    assert letters.language_of("Dear Kannan, we reached Madurai.") == "en"
    assert letters.language_of("நான் bus-இல் வந்தேன்") == "ta"
    assert letters.language_of("") == "en"


def test_a_letter_is_read_sentence_by_sentence_with_a_longer_pause_between_paragraphs():
    found = letters.sentences(LETTER)
    assert [s["text"] for s in found] == [
        "அன்புள்ள கண்ணன்,",
        "நாங்கள் 1962 மார்ச் 12 அன்று மதுரை வந்து சேர்ந்தோம்.",
        "அம்மா உன்னை நினைத்துக்கொண்டே இருக்கிறாள்.",
        "ஆசீர்வாதங்களுடன், அப்பா"]
    assert [s["pause"] for s in found] == [letters.SENTENCE_PAUSE, letters.SENTENCE_PAUSE,
                                           letters.PARAGRAPH_PAUSE, 0.0]


def test_a_run_on_sentence_is_cut_at_a_comma_and_never_beyond_the_longest_piece():
    long = ", ".join(["the mango tree in the back garden has flowered again this year"] * 8) + "."
    pieces = [s["text"] for s in letters.sentences(long)]
    assert len(pieces) > 1 and all(len(p) <= letters.MAX_PIECE for p in pieces)
    assert all(p.endswith((",", ".")) for p in pieces[:-1]), "cut where a reader would breathe"


def test_english_words_in_a_tamil_sentence_are_their_own_runs():
    assert letters.runs("நான் bus-இல் வந்தேன்") == [("ta", "நான் "), ("en", "bus-"), ("ta", "இல் வந்தேன்")]
    assert letters.runs("Dear Kannan,") == [("en", "Dear Kannan,")]


def test_numbers_are_said_as_words():
    assert letters.english_number("1962") == "nineteen sixty two"
    assert letters.english_number("1905") == "nineteen oh five"
    assert letters.english_number("2005") == "two thousand and five"
    assert letters.english_number("12") == "twelve"
    assert letters.english_number("345") == "three hundred and forty five"
    assert letters.english_number("600001") == "six zero zero zero zero one"
    assert letters.tamil_number("12") == "ஒன்று இரண்டு"
    assert "1962" not in letters.say_numbers("in 1962", "ta")


def test_what_the_voice_is_handed_has_only_what_it_can_say():
    vocabulary = set("abcdefghijklmnopqrstuvwxyz '")
    assert letters.spoken("On 12 March, 1962!", "en", vocabulary) == \
        "On twelve March nineteen sixty two"


def test_the_words_are_kept_tidy_and_a_reading_is_keyed_by_words_language_and_voice():
    assert letters.tidy("a  \r\nb\r\n\r\n\r\n\r\nc  ") == "a\nb\n\nc"
    assert len(letters.tidy("x" * (letters.MAX_WORDS + 50))) == letters.MAX_WORDS
    assert letters.key("a", "ta") != letters.key("a", "en") != letters.key("b", "en")
    assert letters.key("a", "ta") == letters.key("a", "ta")


# --- the voices ----------------------------------------------------------------------

def test_speaking_is_a_capability_of_the_model_host():
    assert CAPABILITIES["speak"] == "ninaivu.runtime.voice:SPEAK"
    assert resolve(CAPABILITIES["speak"]) is voice_mod.SPEAK


def test_both_voices_are_pinned_downloads_and_neither_is_anybodys_own():
    english = model_catalog.MODELS["voice-english"]
    tamil = model_catalog.MODELS["voice-tamil"]
    archive = english["files"][0]
    assert archive["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/")
    assert len(archive["sha256"]) == 64 and set(archive["take"]) == {"model.onnx", "tokens.txt"}
    assert english["requires"] == ["sherpa_onnx"]
    assert tamil["requires"] == ["torch", "transformers"]
    for file in tamil["files"]:
        assert file["repo"] == "facebook/mms-tts-tam" and len(file["revision"]) == 40
        assert len(file.get("sha256") or "") == 64 or len(file.get("git_sha1") or "") == 40
    assert {f["name"] for f in tamil["files"]} >= {"config.json", "vocab.json", "model.safetensors"}
    for entry in (english, tamil):
        assert "CC-BY-NC" in entry["licence"]


class FakeVoice:
    """A voice that says each character as a hundredth of a second of tone."""

    def __init__(self, rate=16000, vocabulary=None):
        self.rate, self.vocabulary, self.said = rate, vocabulary, []

    def say(self, text):
        self.said.append(text)
        return np.full(len(text) * self.rate // 100, 0.3, np.float32)


def test_a_letter_is_written_as_one_reading_with_each_sentence_timed(tmp_path):
    tamil = FakeVoice()
    kept = {"threads": 1, "voices": {"ta": tamil}, "failed": {"en": "not downloaded"}}
    out = tmp_path / "letter.wav"
    made = voice_mod.speak(kept, sentences=letters.sentences(LETTER), language="ta", out=str(out))
    assert len(made["segments"]) == 4 and made["voices"] == ["ta"]
    starts = [s["start"] for s in made["segments"]]
    assert starts == sorted(starts) and starts[0] == 0.0
    gap = made["segments"][3]["start"] - made["segments"][2]["end"]
    assert gap == pytest.approx(letters.PARAGRAPH_PAUSE, abs=0.01)
    assert "1962" not in " ".join(tamil.said), "numbers are said as words"
    assert [bool(s.get("paragraph")) for s in made["segments"]] == [False, False, True, False]
    with wave.open(str(out)) as reading:
        assert reading.getframerate() == 16000 and reading.getnchannels() == 1
        assert reading.getnframes() / 16000 == pytest.approx(made["duration"], abs=0.01)


def test_an_english_word_in_a_tamil_letter_is_read_by_the_english_voice_when_there_is_one(tmp_path):
    tamil, english = FakeVoice(), FakeVoice(vocabulary=set("abcdefghijklmnopqrstuvwxyz "))
    kept = {"threads": 1, "voices": {"ta": tamil, "en": english}, "failed": {}}
    made = voice_mod.speak(kept, sentences=letters.sentences("நான் bus-இல் வந்தேன்."),
                           language="ta", out=str(tmp_path / "a.wav"))
    assert english.said == ["bus"] and made["voices"] == ["en", "ta"]
    alone = {"threads": 1, "voices": {"ta": FakeVoice(vocabulary=set(" நான்இல்வந்தேன"))},
             "failed": {"en": "not downloaded"}}
    voice_mod.speak(alone, sentences=letters.sentences("நான் bus-இல் வந்தேன்."),
                    language="ta", out=str(tmp_path / "b.wav"))
    assert all("bus" not in said for said in alone["voices"]["ta"].said), \
        "without the English voice the English word is left out, not mangled"


def test_nothing_sayable_is_an_error_not_an_empty_file(tmp_path):
    kept = {"threads": 1, "voices": {"en": FakeVoice(vocabulary=set("abc"))}, "failed": {}}
    with pytest.raises(ValueError):
        voice_mod.speak(kept, sentences=[{"text": "!!!", "pause": 0}], language="en",
                        out=str(tmp_path / "x.wav"))
    assert not (tmp_path / "x.wav").exists()


def test_a_missing_voice_says_where_to_get_it(tmp_path, monkeypatch):
    monkeypatch.setattr(model_catalog, "installed", lambda _model: False)
    for language in ("ta", "en"):
        why = voice_mod.ready(language)
        assert why and ("AI models" in why or "Extras" in why)
    assert voice_mod.ready("fr") == "there is no voice for this language"


@pytest.mark.skipif(importlib.util.find_spec("torch") is None
                    or importlib.util.find_spec("transformers") is None,
                    reason="the Tamil voice runs on transformers and PyTorch")
def test_the_tamil_voice_runs_meta_s_model_as_published(tmp_path):
    from transformers import VitsConfig, VitsModel, VitsTokenizer
    vocab = {c: i for i, c in enumerate(["_", " "] + list("அகணனதநமலவளறயஇஉ்ாிுெேைொோ"))}
    (tmp_path / "vocab.json").write_text(json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
    VitsTokenizer(str(tmp_path / "vocab.json"), language="tam", add_blank=True,
                  normalize=True, phonemize=False, is_uroman=False).save_pretrained(str(tmp_path))
    small = VitsConfig(vocab_size=len(vocab), hidden_size=16, num_hidden_layers=1,
                       num_attention_heads=2, ffn_dim=32, flow_size=16, spectrogram_bins=33,
                       upsample_initial_channel=16, upsample_rates=[8, 8],
                       upsample_kernel_sizes=[16, 16], resblock_kernel_sizes=[3],
                       resblock_dilation_sizes=[[1, 3]], prior_encoder_num_flows=1,
                       prior_encoder_num_wavenet_layers=1, posterior_encoder_num_wavenet_layers=1,
                       duration_predictor_num_flows=1, duration_predictor_filter_channels=16,
                       sampling_rate=16000)
    VitsModel(small).save_pretrained(str(tmp_path))
    tamil = voice_mod._TorchVoice(tmp_path, 1)
    assert tamil.rate == 16000 and "க" in tamil.vocabulary
    first, again = tamil.say("அன்புள்ள கண்ணன்"), tamil.say("அன்புள்ள கண்ணன்")
    assert first.dtype == np.float32 and len(first) > 0
    assert np.array_equal(first, again), "the same sentence is read the same way every time"


# --- the switch ----------------------------------------------------------------------

def test_off_by_default_with_the_advanced_settings():
    assert Config().letters_aloud is False
    assert "letters_aloud" in GROUPS["Advanced"]


def _a_photograph(client) -> int:
    items = client.get("/api/assets?limit=50").get_json()["items"]
    return next(i["id"] for i in items if i["kind"] == "picture")


def test_off_every_route_is_not_there(as_admin):
    target = _a_photograph(as_admin)
    assert as_admin.get(f"/api/asset/{target}/letter").status_code == 404
    assert as_admin.put(f"/api/asset/{target}/letter", json={"words": "x"}).status_code == 404
    assert as_admin.post(f"/api/asset/{target}/letter/read").status_code == 404
    assert as_admin.post(f"/api/asset/{target}/letter/voice").status_code == 404
    assert as_admin.get("/api/status").get_json()["moments"]["letters"] is False


# --- in the viewer -------------------------------------------------------------------

class FakeHost:
    """The quick model host, reading with a flat tone."""

    def __init__(self):
        self.calls = []

    def call(self, capability, **kwargs):
        self.calls.append((capability, kwargs))
        kept = {"threads": 1, "voices": {kwargs["language"]: FakeVoice()}, "failed": {}}
        return voice_mod.speak(kept, sentences=kwargs["sentences"], language=kwargs["language"],
                               out=kwargs["out"])


@pytest.fixture()
def lettered(app, scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.letters_aloud = True
    host = FakeHost()
    monkeypatch.setattr(api_letters, "_host", lambda: host)
    monkeypatch.setattr(voice_mod, "ready", lambda language: None)
    return {"cfg": cfg, "conn": conn, "host": host}


def test_a_photograph_with_no_words_says_so_and_offers_both_voices(as_family, lettered):
    target = _a_photograph(as_family)
    assert as_family.get("/api/status").get_json()["moments"]["letters"] is True
    status = as_family.get(f"/api/asset/{target}/letter").get_json()
    assert status["words"] == "" and status["reading"] is None
    assert status["voices"] == {"ta": {"ready": True, "why": ""}, "en": {"ready": True, "why": ""}}


def test_a_guest_is_not_offered_it(as_guest, as_admin, lettered):
    target = _a_photograph(as_admin)
    assert as_guest.get(f"/api/asset/{target}/letter").status_code in (403, 404)
    assert as_guest.put(f"/api/asset/{target}/letter", json={"words": "x"}).status_code in (403, 404)


def test_the_words_read_off_the_photograph_are_offered_not_kept(as_family, lettered, monkeypatch):
    seen = {}

    def read(picture):
        seen["size"] = picture.size
        return {"words": "Dear Kannan,\nWe reached Madurai.", "language": "en",
                "available": True, "lines": 2}
    monkeypatch.setattr(letters, "read_from_photo", read)
    monkeypatch.setattr("ninaivu.media.ocr.available", lambda: True)
    target = _a_photograph(as_family)
    found = as_family.post(f"/api/asset/{target}/letter/read").get_json()
    assert found == {"words": "Dear Kannan,\nWe reached Madurai.", "language": "en", "lines": 2}
    assert seen["size"][0] > 0
    assert letters_store.get(lettered["conn"], target) is None, "nothing kept until the family says"


def test_without_the_text_reader_the_words_can_still_be_typed(as_family, lettered, monkeypatch):
    monkeypatch.setattr("ninaivu.media.ocr.available", lambda: False)
    target = _a_photograph(as_family)
    response = as_family.post(f"/api/asset/{target}/letter/read")
    assert response.status_code == 409 and "typed" in response.get_json()["error"]
    assert as_family.get(f"/api/asset/{target}/letter").get_json()["reader"] is False
    kept = as_family.put(f"/api/asset/{target}/letter", json={"words": LETTER}).get_json()
    assert kept["words"] == LETTER and kept["language"] == "ta" and kept["source"] == "typed"


def test_kept_words_are_read_aloud_once_and_the_reading_kept(as_family, lettered):
    cfg, conn, host = lettered["cfg"], lettered["conn"], lettered["host"]
    target = _a_photograph(as_family)
    assert as_family.post(f"/api/asset/{target}/letter/voice").status_code == 409, \
        "nothing to read before the words are kept"
    as_family.put(f"/api/asset/{target}/letter", json={"words": LETTER, "source": "read"})
    status = as_family.post(f"/api/asset/{target}/letter/voice").get_json()
    assert host.calls[0][0] == "speak" and host.calls[0][1]["language"] == "ta"
    reading = status["reading"]
    assert len(reading["segments"]) == 4 and reading["duration"] > 0
    audio = as_family.get(reading["audio"])
    assert audio.status_code == 200 and audio.mimetype == "audio/wav" and audio.data[:4] == b"RIFF"
    assert as_family.post(f"/api/asset/{target}/letter/voice").get_json()["reading"] == reading
    assert len(host.calls) == 1, "the same words are read once"
    files = list((Path(cfg.state_dir) / "letters").iterdir())
    assert [f.name for f in files] == [f"{target}-{letters.key(LETTER, 'ta')}.wav"]
    assert letters_store.get(conn, target)["source"] == "read"


def test_changed_words_let_the_old_reading_go(as_family, lettered):
    cfg = lettered["cfg"]
    target = _a_photograph(as_family)
    as_family.put(f"/api/asset/{target}/letter", json={"words": LETTER})
    as_family.post(f"/api/asset/{target}/letter/voice")
    changed = as_family.put(f"/api/asset/{target}/letter",
                            json={"words": LETTER + " நன்றி.", "language": "ta"}).get_json()
    assert changed["reading"] is None
    assert list((Path(cfg.state_dir) / "letters").iterdir()) == []
    as_family.post(f"/api/asset/{target}/letter/voice")
    same = as_family.put(f"/api/asset/{target}/letter",
                         json={"words": LETTER + " நன்றி.", "language": "ta"}).get_json()
    assert same["reading"] is not None, "keeping the same words again keeps their reading"
    gone = as_family.delete(f"/api/asset/{target}/letter").get_json()
    assert gone["words"] == "" and list((Path(cfg.state_dir) / "letters").iterdir()) == []


def test_a_busy_host_is_said_and_nothing_is_kept(as_family, lettered, monkeypatch):
    class Busy:
        def call(self, *_a, **_k):
            raise runtime.HostBusy("busy")
    monkeypatch.setattr(api_letters, "_host", lambda: Busy())
    target = _a_photograph(as_family)
    as_family.put(f"/api/asset/{target}/letter", json={"words": LETTER})
    assert as_family.post(f"/api/asset/{target}/letter/voice").status_code == 429
    assert list((Path(lettered["cfg"].state_dir) / "letters").iterdir()) == []


def test_without_the_voice_it_says_where_to_get_it(as_family, lettered, monkeypatch):
    monkeypatch.setattr(voice_mod, "ready",
                        lambda language: "the Tamil voice is not downloaded (AI models page)")
    target = _a_photograph(as_family)
    as_family.put(f"/api/asset/{target}/letter", json={"words": LETTER})
    response = as_family.post(f"/api/asset/{target}/letter/voice")
    assert response.status_code == 409 and "AI models" in response.get_json()["error"]


def test_a_photograph_the_viewer_may_not_see_has_no_letter(as_family, as_admin, lettered):
    conn = lettered["conn"]
    target = _a_photograph(as_admin)
    db.update_asset(conn, target, nsfw=1)
    assert as_family.get(f"/api/asset/{target}/letter").status_code == 404
    assert as_admin.get(f"/api/asset/{target}/letter").status_code == 200, "admins see everything"


def test_a_memory_film_narrates_a_letter_nobody_told_a_story_about(as_family, lettered):
    cfg, conn = lettered["cfg"], lettered["conn"]
    target = _a_photograph(as_family)
    assert api_letters.narration_for(cfg, conn, target) is None
    as_family.put(f"/api/asset/{target}/letter", json={"words": LETTER})
    as_family.post(f"/api/asset/{target}/letter/voice")
    told = api_letters.narration_for(cfg, conn, target)
    assert told["language"] == "ta" and Path(told["path"]).is_file() and told["duration"] > 0
    assert [s["text"] for s in told["segments"]][0] == "அன்புள்ள கண்ணன்,"
    cfg.letters_aloud = False
    assert api_letters.narration_for(cfg, conn, target) is None
