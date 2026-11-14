"""What easy view says about a photograph, in Tamil or English.

Short sentences a grandparent can follow, made only from what the index
already knows and the person may see: who the family named in it, left to
right; where it was taken; the day, and how long ago that was; and a caption
somebody in the family wrote. Nothing is guessed and no model is asked: a
photograph with none of these is "a photograph", and that is all that is
said. A date from the file's own clock (``mtime``, ``created``) says nothing
about when a photograph was taken, so it is never said.

The sentences are what the page shows under the photograph and what the
voice reads (features/letters, ``runtime/voice.py``), or the device's own
voice when the home computer has none.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from typing import Any, Sequence

#: The languages easy view speaks.
LANGUAGES = ("ta", "en")

#: Names said one by one; more than this and the rest are counted.
MOST_NAMES = 4

#: The longest caption read out, in characters.
MAX_CAPTION = 280

#: Where a date comes from that says nothing about when it was taken.
_FILE_DATES = ("mtime", "created", "none", "")

_MONTHS = {
    "en": ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"),
    "ta": ("ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை",
           "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்"),
}
_DAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "ta": ("திங்கட்கிழமை", "செவ்வாய்க்கிழமை", "புதன்கிழமை", "வியாழக்கிழமை", "வெள்ளிக்கிழமை",
           "சனிக்கிழமை", "ஞாயிற்றுக்கிழமை"),
}


def language(value: Any) -> str:
    """*value* if easy view speaks it, else Tamil, the family's own."""
    text = str(value or "").strip().lower()[:2]
    return text if text in LANGUAGES else "ta"


def _date(row: dict[str, Any]) -> _dt.date | None:
    if str(row.get("date_source") or "") in _FILE_DATES:
        return None
    key = str(row.get("date_key") or "").strip()
    try:
        return _dt.date.fromisoformat(key[:10])
    except ValueError:
        return None


def _names(names: Sequence[str], lang: str) -> str:
    shown = [n for n in names if n][:MOST_NAMES]
    rest = max(0, len([n for n in names if n]) - len(shown))
    if lang == "ta":
        if rest:
            return ", ".join(shown) + f" மற்றும் இன்னும் {rest} பேர்"
        if len(shown) > 1:
            return ", ".join(shown[:-1]) + " மற்றும் " + shown[-1]
        return shown[0]
    if rest:
        more = "one other" if rest == 1 else f"{rest} others"
        return ", ".join(shown) + f" and {more}"
    if len(shown) > 1:
        return ", ".join(shown[:-1]) + " and " + shown[-1]
    return shown[0]


def _ago(when: _dt.date, today: _dt.date, lang: str) -> str:
    years = today.year - when.year - ((today.month, today.day) < (when.month, when.day))
    if years >= 1:
        if lang == "ta":
            return f"{years} ஆண்டுகளுக்கு முன்பு." if years > 1 else "ஒரு ஆண்டுக்கு முன்பு."
        return f"That was {years} years ago." if years > 1 else "That was a year ago."
    days = (today - when).days
    if days == 0:
        return "இன்று." if lang == "ta" else "That was today."
    if 0 < days < 365:
        if lang == "ta":
            return "இந்த ஆண்டு."
        return "That was this year."
    return ""


def sentences(row: dict[str, Any], *, names: Sequence[str], lang: str,
              today: _dt.date | None = None) -> list[str]:
    """What easy view says about photograph *row*, as sentences, in *lang*.

    *names* are the people the family named in it, left to right
    (features/photo_talk/media.py ``names_in``)."""
    lang = language(lang)
    today = today or _dt.date.today()
    video = row.get("kind") == "video"
    said: list[str] = []
    if names:
        who = _names(names, lang)
        if lang == "ta":
            said.append(f"இந்த {'காணொளியில்' if video else 'படத்தில்'}: {who}.")
        else:
            said.append(f"In this {'video' if video else 'photograph'}: {who}.")
    place = ", ".join(str(p).strip() for p in (row.get("city"), row.get("country")) if p and str(p).strip())
    if place:
        said.append(f"இடம்: {place}." if lang == "ta" else f"Taken in {place}.")
    when = _date(row)
    if when is not None:
        month = _MONTHS[lang][when.month - 1]
        day = _DAYS[lang][when.weekday()]
        if lang == "ta":
            said.append(f"நாள்: {when.day} {month} {when.year}, {day}.")
        else:
            said.append(f"On {day}, {when.day} {month} {when.year}.")
        if ago := _ago(when, today, lang):
            said.append(ago)
    caption = " ".join(str(row.get("caption") or "").split())
    if caption and row.get("caption_source") == "manual":
        if len(caption) > MAX_CAPTION:
            caption = caption[:MAX_CAPTION].rsplit(" ", 1)[0] + "…"
        if caption[-1] not in ".!?…":
            caption += "."
        said.append(f"குடும்பம் எழுதியது: {caption}" if lang == "ta" else f"The family wrote: {caption}")
    if not said:
        if lang == "ta":
            said.append("ஒரு காணொளி." if video else "ஒரு படம்.")
        else:
            said.append("A video." if video else "A photograph.")
    return said


def key(text: Sequence[str], lang: str) -> str:
    """The name of the reading of exactly these sentences in this language."""
    digest = hashlib.sha256(("\n".join(text) + "\0" + lang).encode("utf-8")).hexdigest()
    return digest[:16]
