"""The family app in Tamil, or English.

Most of what can go wrong here is not a bug in a function, it is a page that
says `gallery.empty.title` to somebody's mother, or a locale file that has
drifted away from the template it is supposed to translate. So these read the
markup and the locale files and check they still agree.

The rules being protected:

* English is the *source*. The keys are English sentences, so a missing
  translation falls back to something readable.
* Every key in the page has a translation, and no locale carries strings the
  page no longer has.
* Nothing that is not this application's words — a folder name on disk, a
  product name — is marked for translation at all.

Where it stops, and why it stops there:

The family app is translated. The admin console is not, and that is a decision
rather than a gap — it was taken deliberately on 2026-09-25. The console is for
whoever runs this installation, which here is one person who reads English;
translating its ~576 strings would be work for nobody, and half-translating it
is worse than leaving it, because a screen in two languages reads as broken.

So `admin.html` and the console's own scripts are absent from PAGES and are
expected to be absent. If that ever changes, the console's files join PAGES and
its strings join the locales — but until somebody who reads Tamil administers
Ninaivu, this boundary is the right one and should not be read as unfinished.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "ninaivu" / "templates" / "index.html"
#: Every template that is translated. `admin.html` is deliberately absent —
#: the console is in English, and listing it here would call every one of its
#: strings a missing translation.
PAGES = (PAGE, ROOT / "ninaivu" / "templates" / "share.html")
LOCALES = ROOT / "ninaivu" / "static" / "i18n"
SCRIPT = ROOT / "ninaivu" / "static" / "js" / "i18n.js"
JS = ROOT / "ninaivu" / "static" / "js"


def page() -> str:
    """Every translated template, read as one. A shared link is the only page
    somebody outside the household ever sees, so it is translated too."""
    return "\n".join(p.read_text(encoding="utf-8") for p in PAGES)


def locale(code: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))


def marked_keys() -> set[str]:
    """Every string the template asks to have translated."""
    source = page()
    keys = set(re.findall(r'data-i18n="([^"]+)"', source))
    # One marker per attribute. A single `data-i18n-attr="title:..."` list
    # split on commas broke on the first string that had a comma in it.
    for marker in ("data-i18n-title", "data-i18n-label",
                   "data-i18n-placeholder"):
        keys |= set(re.findall(rf'{marker}="([^"]+)"', source))
    return {k.replace("&quot;", '"') for k in keys} | asked_for_in_script()


#: `i18n.t('…')` or `i18n.t("…")`. The body excludes only the quote that opened
#: it, so "Who's watching?" is one string rather than a broken pair.
#: `key()` names a key without translating it there — see i18n.js.
ASKED = re.compile(r"""i18n\.(?:t|key)\(\s*(['"])((?:(?!\1)[^\\]|\\.)+?)\1""")


def asked_for_in_script() -> set[str]:
    """Every string the scripts ask to have translated.

    The sign-in screen is built in JavaScript rather than written into the
    template — `.gate` covers the whole page, so the card has to be built — and
    its strings arrive as `i18n.t(...)` calls. A guard that read only the
    template called all of them stale.
    """
    keys: set[str] = set()
    for source in sorted(JS.glob("*.js")):
        for quote, key in ASKED.findall(source.read_text(encoding="utf-8")):
            keys.add(key.replace("\\" + quote, quote))
    return keys


# -- the locales and the page agree -----------------------------------------

def test_the_page_asks_for_something_to_translate():
    assert len(marked_keys()) > 50, "the template has lost its translation markup"


@pytest.mark.parametrize("code", ["ta"])
def test_every_string_in_the_page_is_translated(code):
    missing = sorted(marked_keys() - set(locale(code)))
    assert not missing, f"{code} is missing {len(missing)}: {missing[:8]}"


@pytest.mark.parametrize("code", ["en", "ta"])
def test_a_locale_carries_nothing_the_page_no_longer_says(code):
    """A stale string is a translator's time spent on a page that is gone."""
    stale = sorted(set(locale(code)) - marked_keys())
    assert not stale, f"{code} has {len(stale)} unused: {stale[:8]}"


@pytest.mark.parametrize("code", ["en", "ta"])
def test_nothing_is_translated_to_nothing(code):
    empty = [k for k, v in locale(code).items() if not str(v).strip()]
    assert not empty, f"{code} has empty strings: {empty[:8]}"


def test_english_is_the_source_not_a_translation():
    """Every key *is* its English text, so a fallback is readable."""
    for key, value in locale("en").items():
        assert key == value, f"{key!r} -> {value!r}"


def test_tamil_is_actually_tamil():
    """A file of English copied into place would pass every test above."""
    strings = locale("ta")
    tamil = [v for v in strings.values()
             if re.search(r"[஀-௿]", str(v))]
    assert len(tamil) > len(strings) * 0.8, (
        f"only {len(tamil)} of {len(strings)} contain Tamil letters")


# -- what must never be marked ----------------------------------------------

#: Words that are the same in every language, or are not words at all. A
#: folder Ninaivu makes on disk is named that on disk, whatever the person
#: reading the page speaks.
NEVER_TRANSLATE = ("_deleted", "_deleted/_originals", "Ninaivu", "AI")


@pytest.mark.parametrize("word", NEVER_TRANSLATE)
def test_names_that_are_not_words_are_left_alone(word):
    assert word not in marked_keys()


def test_no_key_runs_over_more_than_one_line():
    """A key with a newline in it is a key nobody can look up in a file."""
    for key in marked_keys():
        assert "\n" not in key and "  " not in key, repr(key)


def test_jinja_is_not_marked_for_translation():
    """The template decides those, and it decides them per request."""
    for key in marked_keys():
        assert "{{" not in key and "{%" not in key, repr(key)


# -- the module's own promises ----------------------------------------------

def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_a_missing_translation_falls_back_to_the_key():
    """`t()` returns the key, which is the English sentence."""
    assert re.search(r"table\[key\]\s*\|\|\s*key", script())


def test_the_choice_is_kept_on_the_device():
    """Same rule as the theme: it has to work before anybody has signed in."""
    assert "localStorage" in script()
    assert "ninaivu.lang" in script()


def test_a_locale_that_will_not_load_leaves_the_page_readable():
    """English is a worse answer than Tamil and a much better one than nothing."""
    assert re.search(r"catch\s*\{[^}]*loaded\[code\]\s*=\s*\{\}", script(), re.S)


def test_both_languages_are_offered_and_named_in_themselves():
    """Somebody looking for Tamil is looking for the word தமிழ்."""
    listed = re.search(r"LANGUAGES = \[(.*?)\];", script(), re.S)
    assert listed
    assert "'en'" in listed.group(1) and "'ta'" in listed.group(1)
    assert "தமிழ்" in listed.group(1)


def test_an_attribute_marker_survives_a_comma_in_the_sentence():
    """The flaw the first format had, in the string that found it."""
    assert 'data-i18n-title="Everyone, including guests"' in page()
    assert "Everyone, including guests" in marked_keys()
    assert "data-i18n-attr=" not in page(), "the ambiguous format is back"


def test_the_page_carries_the_control_and_the_script_finds_it():
    assert 'id="lang-btn"' in page()
    assert 'id="lang-label"' in page()
    app = (ROOT / "ninaivu" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "#lang-btn" in app and "#lang-label" in app
    assert "i18n.start()" in app, "the language is not set before the first paint"


def test_changing_language_leaves_what_the_page_wrote_alone():
    """The library's folder is marked up as "No folder selected", and every
    change of language used to put that back over the real path."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to run the translation script")
    subprocess.run([node, str(Path(__file__).with_name("language_switch.mjs"))],
                   check=True, timeout=30)


def test_every_locale_file_is_valid_json():
    for path in LOCALES.glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


# -- the sign-in screen, which is where somebody arrives --------------------

def test_the_sign_in_screen_asks_for_its_strings_to_be_translated():
    """It is built in JavaScript, so none of it was marked at all: a reader of
    Tamil met an English sign-in screen."""
    asked = asked_for_in_script()
    for line in ("Who's watching?", "Just looking", "Password",
                 "Continue as a guest"):
        assert line in asked, line


def test_the_sign_in_card_carries_a_language_switch():
    """The topbar's switch is not enough. `.gate` is `position: fixed; inset:
    0`, so it covers the control that would have changed the language — which
    left somebody who reads only Tamil with no way through.
    """
    source = (JS / "accounts.js").read_text(encoding="utf-8")
    assert "gate-languages" in source
    assert "i18n.LANGUAGES" in source, "the switch must offer every language"
    assert "i18n.use(" in source, "picking one must actually change it"

    style = (ROOT / "ninaivu" / "static" / "css" / "style.css").read_text(encoding="utf-8")
    assert ".gate-languages" in style, "the switch needs somewhere to sit"


def test_the_gate_still_covers_the_page():
    """If this ever stops being true the test above has lost its reason, and
    the switch on the card is merely a convenience rather than the way in."""
    style = (ROOT / "ninaivu" / "static" / "css" / "style.css").read_text(encoding="utf-8")
    gate = style[style.index(".gate {"):style.index(".gate-card")]
    assert "position: fixed" in gate and "inset: 0" in gate


# -- a key must be the string the app really looks up ------------------------

def test_a_key_is_one_literal_and_not_a_sum_of_two():
    """`i18n.t('a ' + 'b')` looks up "a b" and reads as the key "a ".

    Which means the guard above would collect a key nothing asks for, find it
    translated, and pass while that sentence stayed in English on the screen.
    Two of them were written that way before this test existed.
    """
    joined = re.compile(r"""i18n\.t\(\s*(['"])(?:(?!\1)[^\\]|\\.)+?\1\s*\+""")
    for source in sorted(JS.glob("*.js")):
        text = source.read_text(encoding="utf-8")
        assert not joined.search(text), (
            f"{source.name} builds a key by adding two strings; write it as one")


def test_a_key_is_written_as_the_characters_it_is():
    r"""`'\u2019'` is one escape in the source and one apostrophe at runtime.

    The same mismatch as above, from the other direction: the scanner reads
    six characters, `t()` is handed one.
    """
    escaped = re.compile(r"""i18n\.t\(\s*(['"])(?:(?!\1)[^\\]|\\.)*?\\u[0-9a-fA-F]{4}""")
    for source in sorted(JS.glob("*.js")):
        text = source.read_text(encoding="utf-8")
        assert not escaped.search(text), (
            f"{source.name} has a unicode escape inside a key; write the character")


def test_nothing_is_translated_while_a_file_is_still_loading():
    """`i18n.t()` at module scope runs during import, before `start()` has
    fetched anything — so it returns the key and that English is frozen for
    the life of the page.

    It was `BOOT_STEPS`, whose four lines are the first words anybody sees.
    A default parameter is fine: that is evaluated per call, not at import.
    """
    offenders = []
    for source in sorted(JS.glob("*.js")):
        depth = 0
        for number, line in enumerate(
                source.read_text(encoding="utf-8").split("\n"), start=1):
            at = line.find("i18n.t(")
            if at >= 0:
                before = line[:at]
                # Depth where the call is, not where its line began: a file
                # that writes a whole arrow function on one line has plenty of
                # calls that are nested even though the line starts at zero.
                here = depth + before.count("{") - before.count("}")
                inside_a_function = "=>" in before or "function" in before
                if (here == 0 and not inside_a_function
                        and not re.match(r"\s*(//|\*)", line)):
                    offenders.append(f"{source.name}:{number}")
            depth += line.count("{") - line.count("}")
    assert not offenders, (
        "translated at import, so frozen in English: " + ", ".join(offenders))


# --- the language follows the person -------------------------------------

def test_the_language_is_saved_on_the_profile_and_comes_back_at_sign_in(app, people):
    """Chosen once, the same on the phone, the tablet and the television —
    and the browser's own language is only what a device asks for before
    anybody has signed in."""
    from conftest import FAMILY, login

    client = app.test_client()
    login(client, *FAMILY)
    assert client.get("/api/me").get_json()["language"] == ""
    assert client.post("/api/me", json={"language": "ta"}).status_code == 200
    assert client.get("/api/me").get_json()["language"] == "ta"

    again = app.test_client()
    login(again, *FAMILY)
    assert again.get("/api/me").get_json()["language"] == "ta", "another device, the same choice"

    assert client.post("/api/me", json={"language": "<script>"}).status_code == 400
    assert client.post("/api/me", json={"language": ""}).status_code == 200
    assert client.get("/api/me").get_json()["language"] == "", "back to what the device asks for"


def test_the_page_applies_the_profile_language_and_saves_the_button():
    app_js = (ROOT / "ninaivu" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "state.user.language" in app_js and "rememberLanguage(next)" in app_js


def test_the_lock_screen_offers_the_languages_too():
    """The lock covers the topbar's button; a lock screen in a language you
    cannot read is a locked door with no handle."""
    lock_js = (ROOT / "ninaivu" / "static" / "js" / "lock.js").read_text(encoding="utf-8")
    assert "gate-languages" in lock_js and "i18n.LANGUAGES" in lock_js
