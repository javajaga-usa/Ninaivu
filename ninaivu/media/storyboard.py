"""A memory film's storyboard: its title, its chapters and a line for each.

Plan E of the video plans: Ninaivu drafts the shape of a film the way a
person editing it would, before anybody touches it. The moments are already
chosen and in order (media/films.py picks them); what is drafted is

* a **title** and a **subtitle** for the film, and a **closing** line;
* **chapters**: where the film turns (a new place, a new season, the next
  day of a trip), each with a short heading and a line under it.

Two ways, the same answer shape:

**From what the library knows** (:func:`by_rules`), always available: the
moments are split where they are days apart or the place changes, small
pieces are joined to their neighbours, and each chapter is headed by its
place or its months. Nothing is written that the library does not hold.

**By the on-device writing model** (:func:`by_model`, runtime/llm.py), when
the household has it: it reads each moment's date, place, the people in it,
its description (Florence-2) and the first words of any story told about it,
and writes the headings and lines in Tamil or English. Its answer is held to
a JSON shape while it is written, and checked here (:func:`parse`): chapters
must start in order, inside the film, and every heading must be short. An
answer that fails the check is not used; the rules' draft is.

Everything a person sees is a draft: the Make a film screen shows it to be
changed before the film is made.
"""
from __future__ import annotations

import time
from typing import Any, Sequence

#: The most chapters a film is drafted with, and the fewest moments in one.
MOST_CHAPTERS = 7
FEWEST_IN_CHAPTER = 3
#: Moments this many days apart start a new chapter.
APART_DAYS = 3
#: Longest heading, line, title and closing a draft may have, in characters.
HEADING_CHARS = 60
LINE_CHARS = 140
TITLE_CHARS = 80
#: How much of each story's words the model reads.
STORY_CHARS = 220
DESCRIPTION_CHARS = 220
LANGUAGES = {"en": "English", "ta": "Tamil"}


def _month_name(when: float, months: Sequence[str] | None) -> tuple[str, int]:
    stamp = time.gmtime(when)
    name = months[stamp.tm_mon - 1] if months and len(months) == 12 else time.strftime("%B", stamp)
    return name, stamp.tm_year


def _span_heading(moments: Sequence[dict[str, Any]], months: Sequence[str] | None) -> str:
    whens = [m["when"] for m in moments if m.get("when")]
    if not whens:
        return ""
    (a, ay), (b, by) = _month_name(min(whens), months), _month_name(max(whens), months)
    if (a, ay) == (b, by):
        return f"{a} {ay}"
    if ay == by:
        return f"{a} – {b} {ay}"
    return f"{a} {ay} – {b} {by}"


def _common_place(moments: Sequence[dict[str, Any]]) -> str:
    places = [m.get("place") for m in moments if m.get("place")]
    if not places:
        return ""
    best = max(set(places), key=places.count)
    return best if places.count(best) * 2 >= len(moments) else ""


def _pieces(moments: Sequence[dict[str, Any]]) -> list[list[int]]:
    """Indexes of the moments, split where the film turns."""
    pieces: list[list[int]] = []
    for n, moment in enumerate(moments):
        if not pieces:
            pieces.append([n])
            continue
        before = moments[pieces[-1][-1]]
        gap = (moment.get("when") or 0) - (before.get("when") or 0)
        moved = moment.get("place") and before.get("place") and moment["place"] != before["place"]
        if gap > APART_DAYS * 86400 or moved:
            pieces.append([n])
        else:
            pieces[-1].append(n)
    # Join a piece too small to be a chapter to the neighbour nearest in time;
    # then the closest neighbours, until there are few enough.
    def gap_after(k: int) -> float:
        return (moments[pieces[k + 1][0]].get("when") or 0) - (moments[pieces[k][-1]].get("when") or 0)

    while len(pieces) > 1:
        small = [k for k, piece in enumerate(pieces) if len(piece) < FEWEST_IN_CHAPTER]
        if not small and len(pieces) <= MOST_CHAPTERS:
            break
        if small:
            k = min(small, key=lambda k: len(pieces[k]))
            if k == 0:
                join = 0
            elif k == len(pieces) - 1:
                join = k - 1
            else:
                join = k - 1 if gap_after(k - 1) <= gap_after(k) else k
        else:
            join = min(range(len(pieces) - 1), key=gap_after)
        pieces[join:join + 2] = [pieces[join] + pieces[join + 1]]
    return pieces


def by_rules(moments: Sequence[dict[str, Any]], *, title: str = "",
             months: Sequence[str] | None = None) -> dict[str, Any]:
    """A storyboard from dates and places alone.

    *moments* are the film's, in order: ``id``, ``when`` (seconds), and
    ``place`` where known. A film that does not turn anywhere has no chapters.
    """
    pieces = _pieces(moments) if moments else []
    chapters = []
    if len(pieces) > 1:
        for piece in pieces:
            group = [moments[n] for n in piece]
            heading = _common_place(group) or _span_heading(group, months)
            chapters.append({"id": group[0]["id"], "title": heading[:HEADING_CHARS], "line": ""})
    place = _common_place(moments) if moments else ""
    return {"title": (title or place or _span_heading(moments, months))[:TITLE_CHARS],
            "subtitle": _span_heading(moments, months) if (title or place) else "",
            "closing": "", "chapters": chapters, "by": "rules"}


# ---------------------------------------------------------------------------
# By the writing model
# ---------------------------------------------------------------------------

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "closing": {"type": "string"},
        "chapters": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"starts_at": {"type": "integer"},
                                     "title": {"type": "string"},
                                     "line": {"type": "string"}},
                      "required": ["starts_at", "title", "line"]},
        },
    },
    "required": ["title", "subtitle", "closing", "chapters"],
}


def _moment_line(n: int, moment: dict[str, Any], months: Sequence[str] | None) -> str:
    parts = [f"{n}."]
    if moment.get("when"):
        name, year = _month_name(moment["when"], months)
        parts.append(f"{time.gmtime(moment['when']).tm_mday} {name} {year}")
    if moment.get("kind") == "video":
        parts.append("(a video clip)")
    if moment.get("place"):
        parts.append(f"at {moment['place']}")
    if moment.get("people"):
        parts.append("with " + ", ".join(moment["people"][:6]))
    line = " ".join(parts)
    about = " ".join(str(moment.get("about") or "").split())[:DESCRIPTION_CHARS]
    if about:
        line += f" — {about}"
    story = " ".join(str(moment.get("story") or "").split())[:STORY_CHARS]
    if story:
        line += f' — somebody told a story about it: "{story}"'
    return line


def prompt(moments: Sequence[dict[str, Any]], *, language: str, title: str = "",
           months: Sequence[str] | None = None) -> list[dict[str, str]]:
    """The messages the writing model is given."""
    tongue = LANGUAGES.get(language, "English")
    system = (
        "You help a family make a short film of their own photographs and videos. "
        "You are given the film's moments in the order they will be shown. "
        f"Write in {tongue}, warmly and simply, as a member of the family would. "
        "Never invent names, places, events or feelings that the moments do not show; "
        "when you do not know something, say less. Headings are a few words; lines are one short sentence.")
    listed = "\n".join(_moment_line(n, m, months) for n, m in enumerate(moments))
    ask = (
        f"The film{f' is called {title!r} for now and' if title else ''} has {len(moments)} moments:\n"
        f"{listed}\n\n"
        f"Divide it into 2 to {MOST_CHAPTERS} chapters where it turns (a new place, a new day, "
        f"a new season), each at least {FEWEST_IN_CHAPTER} moments long, in order. "
        "Give each chapter the number of its first moment (starts_at; the first chapter starts at 0), "
        "a heading and one line under it. Give the film a title, a subtitle and a closing line. "
        f"If the film does not turn anywhere, give no chapters. Everything in {tongue}.")
    return [{"role": "system", "content": system}, {"role": "user", "content": ask}]


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip() if isinstance(value, str) else ""


def parse(answer: Any, moments: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The model's answer as a storyboard, or None when it does not hold up:
    chapters out of order, outside the film or too short, or no title."""
    if not isinstance(answer, dict):
        return None
    title = _text(answer.get("title"), TITLE_CHARS)
    if not title:
        return None
    chapters = []
    last = -1
    raw = answer.get("chapters") if isinstance(answer.get("chapters"), list) else []
    for chapter in raw:
        if not isinstance(chapter, dict):
            return None
        at = chapter.get("starts_at")
        if isinstance(at, bool) or not isinstance(at, int) or not last < at < len(moments):
            return None
        heading = _text(chapter.get("title"), HEADING_CHARS)
        if not heading:
            return None
        if chapters and at - last < FEWEST_IN_CHAPTER - 1:
            return None
        chapters.append({"id": moments[at]["id"], "title": heading,
                         "line": _text(chapter.get("line"), LINE_CHARS)})
        last = at
    if len(chapters) == 1 or len(chapters) > MOST_CHAPTERS:
        return None
    if chapters and raw[0].get("starts_at") != 0:
        return None
    return {"title": title, "subtitle": _text(answer.get("subtitle"), TITLE_CHARS),
            "closing": _text(answer.get("closing"), TITLE_CHARS), "chapters": chapters,
            "by": "model"}


def by_model(host: Any, moments: Sequence[dict[str, Any]], *, language: str, title: str = "",
             months: Sequence[str] | None = None, timeout: float = 240.0,
             stop: Any = None) -> dict[str, Any] | None:
    """A storyboard written by the writing model in *host*, or None when it
    cannot be (no model, a failure, an answer that does not hold up)."""
    from ..runtime import HostError, llm                       # noqa: PLC0415
    try:
        answer = host.call("generate", messages=prompt(moments, language=language, title=title,
                                                       months=months),
                           max_tokens=900, temperature=0.5, json_schema=SCHEMA,
                           timeout=timeout, stop=stop)
        board = parse(llm.parse_json(answer.get("text") or ""), moments)
    except (HostError, ValueError):
        return None
    if board is not None:
        board["model"] = answer.get("model") or ""
    return board


def draft(moments: Sequence[dict[str, Any]], *, language: str = "en", title: str = "",
          months: Sequence[str] | None = None, host: Any = None) -> dict[str, Any]:
    """The storyboard: by the writing model when *host* is given and it
    answers well, otherwise from dates and places."""
    if host is not None and len(moments) >= FEWEST_IN_CHAPTER:
        board = by_model(host, moments, language=language, title=title, months=months)
        if board is not None:
            return board
    return by_rules(moments, title=title, months=months)
