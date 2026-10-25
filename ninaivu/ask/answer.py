"""Answering a question from what the library already knows.

Given what a question was read as (ask/question.py), this finds the
photographs it is about, inside the asker's own limits, and works out what
can be said of them: how many, the first and the last, the years, the places
and the people in them, and for the ones shown, their descriptions
(storage/descriptions.py), the words read in them, and what was said in the
voice stories told about them (storage/stories.py, story_words.py).

Then it answers in two ways:

* **plainly** (:func:`plain_lines`), always: a line or two of facts, as
  sentences with blanks the page fills in its own language ("I found {n}
  photos and videos."), so the answer is there with no model at all;
* **in words** (:func:`model_messages`, :func:`checked`), when the writing
  model is downloaded (runtime/llm.py): the facts are handed to it with the
  question, and it writes a few sentences in the question's language. It is
  told to say only what the facts say and to point at the photographs it
  means as ``[#123]``; a photograph it points at that is not among the facts
  is taken out of its answer.

What the model is shown is only what the asker could open, and of that,
nothing about an item only administrators may see: an administrator's
question still counts those, but their dates, places and words never reach
the model (the household's rule that hidden photographs are not read by AI).
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..storage import db
from ..words import said
from .question import Question

#: The most matches a question counts and looks across. A question about
#: more than this many photographs is answered from the first this many in
#: its order, and says "more than".
MAX_MATCHES = 20000
#: How many photographs an answer shows, and hands to the model.
SHOWN = 24
#: The most characters of one description, of the words in one photograph,
#: and of one story, handed to the model.
DESCRIBED_CHARS, READ_CHARS, STORY_CHARS = 300, 160, 400


@dataclass
class Found:
    """The photographs a question is about, and what can be said of them."""

    total: int = 0
    ids: list[int] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    first: float | None = None
    last: float | None = None
    first_id: int | None = None
    last_id: int | None = None
    years: list[tuple[int, int]] = field(default_factory=list)
    places: list[tuple[str, int]] = field(default_factory=list)
    people: list[tuple[int, str, int]] = field(default_factory=list)
    moments: list[dict[str, Any]] = field(default_factory=list)
    capped: bool = False
    #: The dates, years, places and people of only the matches the model may
    #: be told of (below "admins only" and nobody's "Only to me"): what
    #: :func:`facts` hands it. For a family member these are the same as the
    #: ones above; for an administrator the ones above also count hidden and
    #: private photographs, which the plain answer may show them. None when
    #: every match may be told of.
    told: "Found | None" = None


def _chunks(ids: Sequence[int], size: int = 900) -> Iterable[list[int]]:
    for start in range(0, len(ids), size):
        yield list(ids[start:start + size])


def _when(row: dict[str, Any]) -> float | None:
    value = row.get("captured_at") or row.get("mtime")
    return float(value) if value else None


def find(conn: sqlite3.Connection, roots: Sequence[str], viewer: dict[str, Any], q: Question, *,
         ranked: Sequence[int] | None = None, only: Sequence[int] | None = None,
         people_names: dict[int, str] | None = None, include_nsfw: bool = False,
         guest: bool = False, shield: dict[str, Any] | None = None) -> Found:
    """The photographs *q* is about, as *viewer* may see them.

    *ranked* is the picture search's order for the question's words
    (``api._semantic_ids``), when there is one; without it the words are
    searched as the gallery's full-text search does. *only* narrows to those
    items (the ones added lately, for "what is new"). *people_names* are the
    people this viewer can see, the only ones an answer may name.
    """
    filters: dict[str, Any] = {
        "kinds": [q.kind] if q.kind else None,
        "favorites": q.favorites,
        "date_from": q.window.date_from if q.window else "",
        "date_to": q.window.date_to if q.window else "",
        "people": [pid for pid, _ in q.people] or None,
        "place": q.place,
        "include_nsfw": include_nsfw,
        "guest_search": guest,
        **(shield or {}),
    }
    pool: list[int] | None = None
    if ranked is not None:
        pool = list(ranked)
    elif q.text:
        filters["text"] = q.text
    if only is not None:
        keep = set(only)
        pool = [i for i in (pool if pool is not None else only) if i in keep]
    if pool is not None:
        filters["ids"] = pool
        filters["include_companions"] = False
    by_date = q.intent in ("first", "last", "when", "new") or ranked is None
    sort = "date_asc" if q.intent == "first" else "date_desc"
    rows, total = db.query_assets(conn, roots, limit=MAX_MATCHES, offset=0,
                                  columns=("id", "captured_at", "mtime", "visibility",
                                           "private_to"),
                                  sort=sort, **viewer, **filters)
    if pool is not None and not by_date:
        rank = {aid: i for i, aid in enumerate(pool)}
        rows.sort(key=lambda r: rank.get(r["id"], len(rank)))
    found = Found(total=int(total if total >= 0 else len(rows)),
                  ids=[int(r["id"]) for r in rows])
    found.capped = found.total > len(found.ids)
    if not rows:
        return found
    _summarise(conn, found, rows, people_names or {})
    tellable = [r for r in rows if _tellable(r)]
    if len(tellable) < len(rows):
        found.told = Found(total=len(tellable), ids=[int(r["id"]) for r in tellable])
        if tellable:
            _summarise(conn, found.told, tellable, people_names or {})
    shown = found.ids[:SHOWN]
    # The first and the last are always among those shown: they are what
    # "when did we last…" is about.
    for extra in (found.first_id, found.last_id):
        if extra is not None and extra not in shown:
            shown.append(extra)
    full, _ = db.query_assets(conn, roots, ids=shown, limit=len(shown), offset=0,
                              include_nsfw=include_nsfw, with_total=False, **viewer)
    order = {aid: i for i, aid in enumerate(shown)}
    full.sort(key=lambda r: order.get(r["id"], 0))
    found.rows = full
    found.moments = moments(conn, full, people_names or {})
    return found


def _tellable(row: dict[str, Any]) -> bool:
    """Whether the model may be told of *row*: not one only administrators
    may see, and nobody's "Only to me"."""
    return int(row.get("visibility") or 0) < 2 and not row.get("private_to")


def _summarise(conn: sqlite3.Connection, found: Found, rows: Sequence[dict[str, Any]],
               names: dict[int, str]) -> None:
    """The first and the last, the years, the places and the people of *rows*."""
    ids = [int(r["id"]) for r in rows]
    dated = [(when, int(r["id"])) for r in rows if (when := _when(r))]
    if dated:
        found.first, found.first_id = min(dated)
        found.last, found.last_id = max(dated)
        found.years = sorted(Counter(datetime.fromtimestamp(w).year for w, _ in dated).items())
    found.places = _places(conn, ids)
    found.people = _people(conn, ids, names)


def _places(conn: sqlite3.Connection, ids: Sequence[int]) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for piece in _chunks(ids):
        for row in conn.execute(
                f"SELECT city, country FROM assets WHERE id IN ({','.join('?' * len(piece))})",
                piece):
            name = row["city"] or row["country"]
            if name:
                counts[str(name)] += 1
    return counts.most_common(8)


def _people(conn: sqlite3.Connection, ids: Sequence[int],
            names: dict[int, str]) -> list[tuple[int, str, int]]:
    if not names:
        return []
    counts: Counter[int] = Counter()
    for piece in _chunks(ids):
        for row in conn.execute(
                "SELECT person_id, COUNT(DISTINCT asset_id) AS n FROM faces "
                f"WHERE asset_id IN ({','.join('?' * len(piece))}) AND person_id IS NOT NULL "
                "GROUP BY person_id", piece):
            if int(row["person_id"]) in names:
                counts[int(row["person_id"])] += int(row["n"])
    return [(pid, names[pid], n) for pid, n in counts.most_common(8)]


def _cut(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def moments(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]],
            names: dict[int, str]) -> list[dict[str, Any]]:
    """What can be told of each of *rows*, for the model: when, where, who,
    and the words about it. An item only administrators may see is left
    out (the rule that hidden photographs are not read by AI)."""
    told = [r for r in rows if _tellable(r)]
    ids = [int(r["id"]) for r in told]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    described: dict[int, str] = {}
    try:
        for row in conn.execute(
                f"SELECT asset_id, text FROM descriptions WHERE asset_id IN ({marks}) "
                "AND text != ''", ids):
            described[int(row["asset_id"])] = row["text"]
    except sqlite3.OperationalError:
        pass
    who: dict[int, list[str]] = {}
    for row in conn.execute(
            f"SELECT DISTINCT asset_id, person_id FROM faces WHERE asset_id IN ({marks}) "
            "AND person_id IS NOT NULL", ids):
        name = names.get(int(row["person_id"]))
        if name:
            who.setdefault(int(row["asset_id"]), []).append(name)
    said: dict[int, list[str]] = {}
    try:
        stories = conn.execute(
            f"SELECT id, asset_id, speaker, text FROM stories WHERE asset_id IN ({marks}) "
            "ORDER BY id", ids).fetchall()
    except sqlite3.OperationalError:
        stories = []
    words: dict[int, dict[str, Any]] = {}
    if stories:
        from ..storage import story_words                       # noqa: PLC0415
        words = story_words.for_stories(conn, [int(s["id"]) for s in stories])
    for story in stories:
        heard = words.get(int(story["id"])) or {}
        spoken = heard.get("words") or ""
        english = heard.get("english") or ""
        text = " ".join(p for p in (story["text"], spoken,
                                    english if english != spoken else "") if p)
        if text:
            speaker = f"{story['speaker']}: " if story["speaker"] else ""
            said.setdefault(int(story["asset_id"]), []).append(speaker + text)
    out = []
    for row in told:
        aid = int(row["id"])
        when = _when(row)
        item: dict[str, Any] = {"id": aid, "kind": row.get("kind") or ""}
        if when:
            item["date"] = datetime.fromtimestamp(when).strftime("%Y-%m-%d")
        place = ", ".join(p for p in (row.get("city"), row.get("country")) if p)
        if place:
            item["place"] = place
        if who.get(aid):
            item["people"] = who[aid]
        if described.get(aid):
            item["description"] = _cut(described[aid], DESCRIBED_CHARS)
        if row.get("caption"):
            item["caption"] = _cut(row["caption"], DESCRIBED_CHARS)
        if row.get("ocr_text"):
            item["words_in_photo"] = _cut(row["ocr_text"], READ_CHARS)
        if said.get(aid):
            item["story"] = _cut(" / ".join(said[aid]), STORY_CHARS)
        if row.get("folder"):
            item["album_folder"] = _cut(row["folder"], 80)
        out.append(item)
    return out


def _day(when: float | None) -> str:
    return datetime.fromtimestamp(when).strftime("%Y-%m-%d") if when else ""


def plain_lines(q: Question, found: Found, *, since: float | None = None) -> list[dict[str, Any]]:
    """The answer without a model: sentences with blanks, ``{"key", "params"}``,
    for the page to put in its own language (static/i18n). Dates are given as
    ``YYYY-MM-DD`` for the page to write out."""
    lines: list[dict[str, Any]] = []

    def say(key: str, **params: Any) -> None:
        lines.append({"key": key, "params": params})

    if q.intent == "new":
        if not found.total:
            say(said("Nothing new has been added since {date}."), date=_day(since))
        else:
            say(said("{n} new photos and videos have been added since {date}."),
                n=found.total, date=_day(since))
        return lines
    if not found.total:
        say(said("I could not find any photos for that."))
        return lines
    n = found.total
    if found.capped:
        say(said("I found more than {n} photos and videos."), n=len(found.ids))
    else:
        say(said("I found {n} photos and videos.") if n != 1 else said("I found one."), n=n)
    first_place = _place_of(found, found.first_id)
    last_place = _place_of(found, found.last_id)
    if q.intent == "last" and found.last:
        if last_place:
            say(said("The most recent is from {date}, in {place}."), date=_day(found.last),
                place=last_place)
        else:
            say(said("The most recent is from {date}."), date=_day(found.last))
    elif q.intent == "first" and found.first:
        if first_place:
            say(said("The first is from {date}, in {place}."), date=_day(found.first),
                place=first_place)
        else:
            say(said("The first is from {date}."), date=_day(found.first))
    elif found.first and found.last and _day(found.first) != _day(found.last):
        say(said("They were taken between {first} and {last}."), first=_day(found.first),
            last=_day(found.last))
    elif found.first:
        say(said("They were taken on {date}."), date=_day(found.first))
    if q.intent == "when" and len(found.years) > 1:
        year, count = max(found.years, key=lambda y: y[1])
        say(said("Most are from {year} ({n})."), year=year, n=count)
    if q.intent in ("where", "show", "when", "count") and found.places:
        say(said("Mostly in {places}."), places=", ".join(p for p, _ in found.places[:3]))
    if q.intent in ("who", "show", "count") and found.people:
        asked = {pid for pid, _ in q.people}
        others = [name for pid, name, _ in found.people if pid not in asked][:4]
        if others:
            say(said("With {people}."), people=", ".join(others))
    return lines


def _place_of(found: Found, aid: int | None) -> str:
    for row in found.rows:
        if row["id"] == aid:
            return str(row.get("city") or row.get("country") or "")
    return ""


# -- the writing model ----------------------------------------------------------

def facts(q: Question, found: Found, *, since: float | None = None) -> dict[str, Any]:
    """Everything the model is told, as one JSON-ready value.

    The count is of every match; the dates, years, places and people are of
    only the ones the model may be told of (``found.told``), so an
    administrator's question never hands it where or when a hidden or private
    photograph was taken, nor who is in it."""
    told = found.told if found.told is not None else found
    out: dict[str, Any] = {
        "matching_items": found.total if not found.capped else f"more than {len(found.ids)}",
        "first_date": _day(told.first) or None,
        "last_date": _day(told.last) or None,
        "items_per_year": {str(y): n for y, n in told.years[-15:]},
        "top_places": [{"place": p, "items": n} for p, n in told.places],
        "people_in_them": [{"name": name, "items": n} for _, name, n in told.people],
        "examples": found.moments,
    }
    understood = {}
    if q.people:
        understood["people"] = [name for _, name in q.people]
    if q.place:
        understood["place"] = q.place
    if q.window is not None:
        understood["dates"] = [q.window.date_from, q.window.date_to]
    if q.text:
        understood["looking_for"] = q.text
    if q.intent == "new" and since:
        understood["added_since"] = _day(since)
    out["search_used"] = understood
    return out


def model_messages(q: Question, found: Found, *, since: float | None = None,
                   household: str = "") -> list[dict[str, str]]:
    """The writing model's instructions for answering *q* from *found*."""
    import json                                                   # noqa: PLC0415
    language = "Tamil (தமிழ்)" if q.language == "ta" else "English"
    system = (
        f"You are Ninaivu, the {household + ' ' if household else ''}family's photo library, "
        "running on their own computer. Answer the family's question using ONLY the facts "
        "given (counts, dates, places, people, descriptions, words in photos, voice stories). "
        f"Reply in {language}, in at most four short, warm, plain sentences. Write dates the "
        "way people say them. When a particular photo helps, point to it as [#id] using the "
        "ids in the facts. If the facts do not answer the question, say so kindly and say what "
        "they do show. Never guess names, places, dates or events that are not in the facts; "
        "descriptions were written by a computer and may be wrong about details.")
    user = f"Question: {q.asked}\n\nFacts (JSON):\n" + json.dumps(
        facts(q, found, since=since), ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_CITED = re.compile(r"\[#(\d+)\]")


def checked(text: str, allowed: Iterable[int]) -> tuple[str, list[int]]:
    """The model's answer with every ``[#id]`` it may not point at taken out,
    and the ones it may, in the order it first named them."""
    allowed = set(int(a) for a in allowed)
    cited: list[int] = []

    def keep(match: re.Match[str]) -> str:
        aid = int(match.group(1))
        if aid not in allowed:
            return ""
        if aid not in cited:
            cited.append(aid)
        return match.group(0)

    cleaned = _CITED.sub(keep, text or "")
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, cited


def took(started: float) -> float:
    return round(time.monotonic() - started, 3)
