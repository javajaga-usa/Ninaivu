"""Letters read aloud: the words on an old letter, written out and spoken.

A family keeps letters, recipe cards, the back of a wedding invitation, a
note in a grandfather's hand. Photographed, they sit in the library as
pictures: nobody can search them, and a grandchild who never learnt to read
Tamil script cannot read them at all. ``letters_aloud`` (Settings →
Advanced) gives each such photograph its words, written out under it, and a
calm voice that reads them aloud in Tamil or English. Everything happens on
this computer; no photograph and no word leaves it.

Where the words come from:

* **The text reader already in the app** (media/ocr.py, PP-OCR) reads a
  printed page well, in English; it is given the photograph upright and
  larger than the scan's own pass reads it, and its lines are kept in order.
* **The family**, who type the words, or correct what was read. A
  handwritten Tamil letter is seldom read well by any reader, so the words
  are always theirs to change, and what they typed is never written over.

Where the voice comes from (runtime/voice.py, in the quick model host):
Meta's MMS text-to-speech models, one for Tamil and one for English, both
downloaded only from the AI models page. No voice is ever made to sound like
a member of the family: the voices are the models' own.

What this module holds is everything that needs no model: which language a
text is in, how a letter is cut into the sentences the voice reads one at a
time (with a pause between them, longer between paragraphs, so a letter is
read the way a person reads one aloud), how numbers are said, and the key a
reading is kept under, so the same words in the same voice are spoken once.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

#: Raised when a change here or in the voices should make every kept reading
#: be spoken afresh.
VOICE_VERSION = 1

#: The longest letter kept and read, in characters: a few pages.
MAX_WORDS = 20000
#: The longest piece handed to the voice at once. MMS reads a sentence well;
#: a run-on line of a hundred words drifts and slurs, so a long sentence is
#: cut at a comma, else at a space.
MAX_PIECE = 220
#: Silence between sentences, and between paragraphs, in seconds.
SENTENCE_PAUSE = 0.45
PARAGRAPH_PAUSE = 0.9
#: The languages there is a voice for.
LANGUAGES = ("ta", "en")

_TAMIL = re.compile(r"[஀-௿]")
_LATIN = re.compile(r"[A-Za-z]")
#: Where a sentence ends: a full stop, a question or exclamation mark, the
#: Indian danda, and a line that ends with nothing at all (a letter's
#: "Dear Kannan," on its own line is a sentence of its own when read).
_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+|\n")
_PARAGRAPH = re.compile(r"\n\s*\n+")


def language_of(text: str) -> str:
    """``ta`` when most of the letters in *text* are Tamil script, else ``en``."""
    tamil = len(_TAMIL.findall(text or ""))
    latin = len(_LATIN.findall(text or ""))
    return "ta" if tamil and tamil >= latin else "en"


def tidy(text: str) -> str:
    """The words as kept: Windows line ends made plain, trailing spaces gone,
    no more than one empty line in a row, cut at :data:`MAX_WORDS`."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:MAX_WORDS]


def key(words: str, language: str) -> str:
    """What a reading of *words* in *language* is kept under."""
    digest = hashlib.sha256(f"{VOICE_VERSION}\0{language}\0{words}".encode("utf-8"))
    return digest.hexdigest()[:20]


# ---------------------------------------------------------------------------
# Numbers, said aloud
# ---------------------------------------------------------------------------

_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
#: Tamil digits by name. A year said digit by digit is not how a person says
#: it, but it is understood, where the voice would otherwise say nothing at
#: all for a number: the Tamil model knows only Tamil letters.
_TAMIL_DIGITS = ("பூஜ்ஜியம்", "ஒன்று", "இரண்டு", "மூன்று", "நான்கு", "ஐந்து", "ஆறு", "ஏழு",
                 "எட்டு", "ஒன்பது")


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    if not hundreds:
        return _below_hundred(rest)
    said = f"{_ONES[hundreds]} hundred"
    return said + (f" and {_below_hundred(rest)}" if rest else "")


def english_number(digits: str) -> str:
    """A number as an English speaker says it: years as years ("nineteen
    sixty-two"), anything else up to 9999 in words, longer numbers (a phone
    number, a pin code) digit by digit."""
    n = int(digits)
    if len(digits) > 4 or (digits.startswith("0") and len(digits) > 1):
        return " ".join(_ONES[int(d)] for d in digits)
    if 1100 <= n <= 1999 or (2010 <= n <= 2099):
        high, low = divmod(n, 100)
        if low == 0:
            return f"{_below_hundred(high)} hundred"
        return f"{_below_hundred(high)} {('oh ' + _ONES[low]) if low < 10 else _below_hundred(low)}"
    if n >= 1000:
        thousands, rest = divmod(n, 1000)
        said = f"{_ONES[thousands]} thousand"
        if rest:
            said += (" and " if rest < 100 else " ") + _below_thousand(rest)
        return said
    return _below_thousand(n)


def tamil_number(digits: str) -> str:
    return " ".join(_TAMIL_DIGITS[int(d)] for d in digits)


def say_numbers(text: str, language: str) -> str:
    """*text* with every run of digits written as words in *language*."""
    convert = tamil_number if language == "ta" else english_number
    return re.sub(r"\d+", lambda m: f" {convert(m.group(0))} ", text)


# ---------------------------------------------------------------------------
# A letter cut into what the voice reads
# ---------------------------------------------------------------------------

def _cut_long(sentence: str, most: int = MAX_PIECE) -> list[str]:
    """*sentence* in pieces of at most *most* characters, cut at a comma or
    semicolon where there is one, else at a space."""
    if len(sentence) <= most:
        return [sentence]
    pieces: list[str] = []
    rest = sentence
    while len(rest) > most:
        window = rest[:most]
        cut = max(window.rfind(","), window.rfind(";"), window.rfind("،"))
        if cut < most // 3:
            cut = window.rfind(" ")
        if cut < most // 3:
            cut = most - 1
        pieces.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def runs(sentence: str) -> list[tuple[str, str]]:
    """*sentence* as runs of one script each: ``[("ta", "…"), ("en", "…")]``.

    A Tamil letter often has an English word in it ("bus", "B.Com", a name),
    and an English one a Tamil word; each voice knows only its own letters
    and would skip the other's. Spaces, digits and punctuation stay with the
    run they are in; a sentence in one script is one run.
    """
    found: list[tuple[str, str]] = []
    current, text = "", ""
    for char in sentence:
        script = "ta" if _TAMIL.match(char) else "en" if _LATIN.match(char) else ""
        if script and current and script != current:
            found.append((current, text))
            text = ""
        if script:
            current = script
        text += char
    if text.strip():
        found.append((current or language_of(sentence), text))
    # A run of a single letter between two of the other script ("Kannan-ம்")
    # is read by the voice around it rather than split off.
    merged: list[tuple[str, str]] = []
    for script, part in found:
        if merged and (merged[-1][0] == script or len(part.strip()) <= 1):
            merged[-1] = (merged[-1][0], merged[-1][1] + part)
        else:
            merged.append((script, part))
    return [(script, part) for script, part in merged if part.strip()]


def sentences(words: str) -> list[dict[str, Any]]:
    """The letter as the sentences the voice reads, in order.

    Each is ``{"text": shown, "pause": seconds of silence after it}``; the
    last of a paragraph is followed by the longer pause. *text* is the
    sentence as written, for the page to light up while it is read.
    """
    found: list[dict[str, Any]] = []
    for paragraph in _PARAGRAPH.split(tidy(words)):
        parts: list[str] = []
        for sentence in _SENTENCE_END.split(paragraph):
            sentence = " ".join(sentence.split())
            if sentence and re.search(r"\w", sentence):
                parts.extend(_cut_long(sentence))
        for n, part in enumerate(parts):
            found.append({"text": part,
                          "pause": PARAGRAPH_PAUSE if n == len(parts) - 1 else SENTENCE_PAUSE})
    if found:
        found[-1]["pause"] = 0.0
    return found


def spoken(text: str, language: str, vocabulary: Iterable[str] | None = None) -> str:
    """*text* as the voice is handed it: numbers in words, and with a
    *vocabulary*, every character the voice does not know made a space (a
    full stop, a quotation mark, a letter of the other script), so it is
    skipped quietly rather than said as noise."""
    text = say_numbers(text, language)
    if vocabulary is not None:
        known = set(vocabulary)
        text = "".join(c if c in known or c.lower() in known else " " for c in text)
    return " ".join(text.split())


def read_from_photo(picture: Any) -> dict[str, Any]:
    """The words the text reader finds on *picture* (an upright PIL image).

    Read larger than the scan's own pass reads a photograph, and with room
    for a page: a letter is all text. ``{"words": "", "language": "en"}``
    when nothing could be read; ``available`` says whether a reader is
    installed at all.
    """
    from . import ocr                                            # noqa: PLC0415
    if not ocr.available():
        return {"words": "", "language": "en", "available": False, "lines": 0}
    found = ocr.read(picture, max_chars=MAX_WORDS, edge=LETTER_EDGE)
    words = tidy(found.get("text") or "")
    return {"words": words, "language": language_of(words), "available": True,
            "lines": int(found.get("lines") or 0)}


#: The long edge a letter is read at: handwriting and small print need more
#: than the 1600 pixels the scan reads a photograph at.
LETTER_EDGE = 2600
