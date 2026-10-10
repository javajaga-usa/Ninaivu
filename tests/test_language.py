"""Ninaivu in Tamil, or English — the family app and the admin console.

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

The console, and why it is in here now:

The family app is translated, and so is the admin console. The console was
left in English on purpose on 2026-09-25 — it was for whoever runs the
installation, who read English. On 2026-09-29 the owner decided otherwise
and the console gets full Tamil support.

So `admin.html` is in PAGES and its strings are in the locales like every
other page's, and the console's scripts ask for theirs with `i18n.t()` —
which `asked_for_in_script` already finds, since it reads every script. The
rule against a screen in two languages still stands; it is now kept by
translating all of the console rather than none of it.
"""

from __future__ import annotations

import html
import html.parser
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "ninaivu" / "templates" / "index.html"
#: Every template that is translated: the family app, a shared link, and —
#: since 2026-09-29 — the admin console.
PAGES = (PAGE, ROOT / "ninaivu" / "templates" / "share.html",
         ROOT / "ninaivu" / "templates" / "admin.html")
LOCALES = ROOT / "ninaivu" / "static" / "i18n"
SCRIPT = ROOT / "ninaivu" / "static" / "js" / "i18n.js"
JS = ROOT / "ninaivu" / "static" / "js"


#: Scripts that cannot ask for a translation: a third-party library, and the
#: workers, which run where i18n.js is not loaded.
NOT_OURS = {"leaflet.js", "editor-worker.js", "worker.mjs"}


def scripts() -> list[Path]:
    """Every script the app writes, subfolders included: Sudar lives in
    `ai-playground/` and its `.mjs` modules, which a top-level `*.js` glob
    never read."""
    found = [*JS.rglob("*.js"), *JS.rglob("*.mjs")]
    return sorted(p for p in found if p.name not in NOT_OURS)


def page() -> str:
    """Every translated template, read as one. A shared link is the only page
    somebody outside the household ever sees, so it is translated too."""
    return "\n".join(p.read_text(encoding="utf-8") for p in PAGES)


def locale(code: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))


def rich_keys(source: str) -> list[str]:
    """Every `data-i18n-rich` sentence, as the browser will hand it to
    i18n.js: the attribute is written `&lt;strong&gt;…` so the page stays
    valid, and it is the decoded `<strong>…` that is looked up."""
    return [html.unescape(k) for k in re.findall(r'data-i18n-rich="([^"]+)"', source)]


def marked_keys() -> set[str]:
    """Every string the template asks to have translated."""
    source = page()
    keys = set(re.findall(r'data-i18n="([^"]+)"', source))
    # One marker per attribute. A single `data-i18n-attr="title:..."` list
    # split on commas broke on the first string that had a comma in it.
    for marker in ("data-i18n-title", "data-i18n-label",
                   "data-i18n-placeholder"):
        keys |= set(re.findall(rf'{marker}="([^"]+)"', source))
    # Decoded, as the browser hands every attribute to i18n.js: a key kept
    # as "Review &amp;amp; clean up" was looked up as "Review & clean up",
    # never found, and the button stayed in English in Tamil.
    keys = {html.unescape(k) for k in keys} | set(rich_keys(source))
    return (keys | asked_for_in_script()
            | said_by_the_server())


#: `said("…")` or `said('…')`: fixed English the server sends and the console
#: translates, marked where it is written — see ninaivu/words.py. Read the same
#: way as `ASKED`; `def said(` is some other function of that name.
SAID = re.compile(r"""(?<!def )\bsaid\(\s*(['"])((?:(?!\1)[^\\]|\\.)+?)\1""")


def python_sources() -> list[Path]:
    return sorted([*(ROOT / "ninaivu").rglob("*.py"), *(ROOT / "extensions").rglob("*.py")])


def said_by_the_server() -> set[str]:
    """Every string the server's modules mark for the console to translate."""
    keys: set[str] = set()
    for source in python_sources():
        for quote, key in SAID.findall(source.read_text(encoding="utf-8")):
            keys.add(key.replace("\\" + quote, quote))
    return keys


def test_said_is_given_one_literal_and_nothing_else():
    """`said(f"…")`, `said(name)` or `said("a " + b)` marks nothing the guard
    can see, or marks the wrong string — the same failure as a JS key built
    from two strings. One plain literal, closed on the same line."""
    call = re.compile(r"(?<!def )\bsaid\(")
    whole = re.compile(r"""\s*(['"])(?:(?!\1)[^\\\n]|\\.)+\1\s*\)""")
    loose = []
    for source in python_sources():
        text = source.read_text(encoding="utf-8")
        for found in call.finditer(text):
            if not whole.match(text, found.end()):
                line = text.count("\n", 0, found.start()) + 1
                loose.append(f"{source.relative_to(ROOT)}:{line}")
    assert not loose, "said() must be given one literal: " + ", ".join(loose)


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
    for source in scripts():
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


#: A tag in a `data-i18n-rich` string: i18n.js knows these and nothing else.
RICH_TAG = re.compile(r"</?(?:strong|em|b|i|code|kbd|a|span)>")


def test_a_rich_translation_carries_the_markup_of_its_english():
    """i18n.js matches a translation's tags one for one to the elements the
    English was written with — the first `<a>` is the first link — and shows
    the English when they differ. So a Tamil sentence that lost its `<strong>`
    would not be seen at all; this finds it first."""
    from collections import Counter
    ta = locale("ta")
    for key in rich_keys(page()):
        if key not in ta:
            continue  # the test above names it
        assert Counter(RICH_TAG.findall(ta[key])) == Counter(RICH_TAG.findall(key)), (
            f"{key[:60]!r}: {ta[key][:60]!r}")
        assert "<" not in RICH_TAG.sub("", ta[key]), f"markup i18n.js will refuse: {ta[key][:60]!r}"


class _Rich(html.parser.HTMLParser):
    """Each `data-i18n-rich` element's English, written the way its key is."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []
        self.open: list[list] = []   # [key, text-so-far, depth, opaque-depth]

    def handle_starttag(self, tag, attrs):
        if self.open:
            rich = self.open[-1]
            rich[2] += 1
            if rich[3] or not RICH_TAG.fullmatch(f"<{tag}>"):
                rich[3] += 1   # an icon, say: not words, not in the key
            else:
                rich[1] += f"<{tag}>"
        elif "data-i18n-rich" in dict(attrs):
            self.open.append([dict(attrs)["data-i18n-rich"], "", 0, 0])

    def handle_startendtag(self, tag, attrs):
        pass   # an svg's <path/>, inside something already opaque

    def handle_endtag(self, tag):
        if not self.open:
            return
        rich = self.open[-1]
        if rich[2] == 0:
            self.found.append((rich[0], re.sub(r"\s+", " ", rich[1]).strip()))
            self.open.pop()
            return
        rich[2] -= 1
        if rich[3]:
            rich[3] -= 1
        else:
            rich[1] += f"</{tag}>"

    def handle_data(self, data):
        if self.open and not self.open[-1][3]:
            self.open[-1][1] += data.replace("<", "&lt;").replace(">", "&gt;")


def test_a_rich_key_is_the_english_it_marks():
    """The key is written out by hand beside the sentence it names, so the two
    can drift: edit the English and the Tamil goes on translating the old
    sentence. An empty tag in the key (`<code></code>`) stands for whatever is
    in it — a `{{image}}`, an icon — which the translation leaves alone."""
    parser = _Rich()
    parser.feed(page())
    assert parser.found, "no data-i18n-rich markup found"
    for key, english in parser.found:
        pattern = "".join(
            re.escape(part) if i % 2 == 0 else f"<{part}>(?:(?!</{part}>).)*</{part}>"
            for i, part in enumerate(re.split(r"<([a-z]+)></\1>", key)))
        assert re.fullmatch(pattern, english, re.S), f"{key[:60]!r} is not {english[:60]!r}"


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
#: reading the page speaks. The name itself is *not* here: Ninaivu is a
#: Tamil word, and a Tamil reader sees நினைவு, in the brand and in every
#: sentence — see the test below.
NEVER_TRANSLATE = ("_deleted", "_deleted/_originals", "AI")


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
    for source in scripts():
        text = source.read_text(encoding="utf-8")
        assert not joined.search(text), (
            f"{source.name} builds a key by adding two strings; write it as one")


def test_a_key_is_written_as_the_characters_it_is():
    r"""`'\u2019'` is one escape in the source and one apostrophe at runtime.

    The same mismatch as above, from the other direction: the scanner reads
    six characters, `t()` is handed one.
    """
    escaped = re.compile(r"""i18n\.t\(\s*(['"])(?:(?!\1)[^\\]|\\.)*?\\u[0-9a-fA-F]{4}""")
    for source in scripts():
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
    for source in scripts():
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


def test_in_tamil_the_name_is_written_in_tamil():
    """நினைவு is a Tamil word before it is a product name. "Ninaivu
    பூட்டப்பட்டுள்ளது" was half a sentence in each script."""
    import json
    ta = json.loads((ROOT / "ninaivu" / "static" / "i18n" / "ta.json").read_text(encoding="utf-8"))
    assert ta["Ninaivu"] == "நினைவு"
    latin = [k for k, v in ta.items() if "Ninaivu" in v]
    assert latin == [], f"still in Latin letters: {latin[:5]}"
    assert ta["Ninaivu is locked"] == "நினைவு பூட்டப்பட்டுள்ளது"
    page = PAGE.read_text(encoding="utf-8")
    assert '<span data-i18n="Ninaivu">Ninaivu</span>' in page, "the brand in the top bar follows too"


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
