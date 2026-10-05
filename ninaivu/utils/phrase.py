"""Reading who, where and what out of a search phrase.

:mod:`ninaivu.utils.query` already takes the dates out of "beach last summer".
This does the same for the rest of what people type into a family library:
"Maya and Arjun at Ooty", "Grandma videos", "favourites from Chennai". A name
the library knows becomes a person filter, a place it has photographs from
becomes a place filter, "videos" and "photos" narrow the kind, and whatever
is left — "at the beach" — goes on to the full-text and picture search, which
is what those are good at.

The same three rules as the date reader:

* **Only what it is sure of.** A word becomes a filter only when it is the
  whole name of a person or place this viewer can already see, or a first
  name nobody else shares. Anything else stays in the phrase, because a wrong
  filter returns nothing and looks like a broken library.
* **A choice made on screen wins.** The caller passes what the page already
  set (a person chosen from the sidebar, the Videos view) and those words are
  left alone.
* **It says what it understood**, so the page can show it as chips a person
  can see — and the words it used are removed from the phrase, not guessed at
  twice.

Nothing here touches the database; the caller hands in the names and places,
already narrowed to what this viewer may see. A person or town that exists
only in photographs somebody may not open is therefore never matched, and
matching it could not be used to learn that it exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

__all__ = ["Understood", "parse", "KIND_WORDS"]

#: Words that mean a kind of item. Plural and singular; "clips" because
#: people say it.
KIND_WORDS = {
    "video": "video", "videos": "video", "clip": "video", "clips": "video",
    "movie": "video", "movies": "video",
    "photo": "picture", "photos": "picture", "picture": "picture",
    "pictures": "picture", "pic": "picture", "pics": "picture",
    "photograph": "picture", "photographs": "picture",
}

_FAVOURITE_WORDS = {"favourite", "favourites", "favorite", "favorites", "starred"}

#: Joining words left behind once the names are taken out. Removed only at
#: the edges of what is left: "Arjun at the beach" leaves "beach", "Maya and
#: Arjun" leaves nothing, and "cake and candles" keeps its "and".
_JOINERS = {"a", "an", "and", "at", "by", "from", "in", "of", "on", "the",
            "with", "near", "&", "+", ",", "my", "our", "all", "me", "us",
            "some", "any", "taken", "shot", "only"}

_WORD = re.compile(r"[\w'’&+-]+", re.UNICODE)


@dataclass
class Understood:
    """What a phrase was read as."""

    text: str
    people: list[tuple[int, str]] = field(default_factory=list)
    place: str = ""
    kind: str = ""
    favorites: bool = False

    def chips(self) -> list[dict[str, str]]:
        """The understood parts, in the order a person would say them."""
        out = [{"type": "person", "label": name, "id": str(pid)} for pid, name in self.people]
        if self.place:
            out.append({"type": "place", "label": self.place})
        if self.kind:
            out.append({"type": "kind", "label": "Videos" if self.kind == "video" else "Photos"})
        if self.favorites:
            out.append({"type": "favorites", "label": "Favourites"})
        return out

    @property
    def matched(self) -> bool:
        return bool(self.people or self.place or self.kind or self.favorites)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip().casefold()


def _names(people: Iterable[tuple[int, str]]) -> dict[str, tuple[int, str]]:
    """Every spelling that may stand for one person: the full name always, and
    the first name when no other person this viewer can see shares it."""
    full: dict[str, tuple[int, str]] = {}
    firsts: dict[str, list[tuple[int, str]]] = {}
    for pid, name in people:
        name = (name or "").strip()
        if not name:
            continue
        key = _norm(name)
        if len(key) < 2:
            continue
        full.setdefault(key, (int(pid), name))
        first = key.split(" ")[0]
        if first != key and len(first) >= 3:
            firsts.setdefault(first, []).append((int(pid), name))
    for first, owners in firsts.items():
        if len({pid for pid, _ in owners}) == 1 and first not in full:
            full[first] = owners[0]
    return full


def _take(words: list[str], spans: list[bool], phrase: str) -> bool:
    """Mark the first unclaimed run of *words* equal to *phrase*; say if found."""
    target = phrase.split(" ")
    size = len(target)
    for start in range(0, len(words) - size + 1):
        if any(spans[start:start + size]):
            continue
        if [w.casefold() for w in words[start:start + size]] == target:
            for i in range(start, start + size):
                spans[i] = True
            return True
    return False


def parse(text: str, *, people: Iterable[tuple[int, str]] = (),
          places: Iterable[str] = (), kind_set: bool = False,
          person_set: bool = False, favorites_set: bool = False) -> Understood:
    """Read people, a place, a kind and "favourites" out of *text*.

    *people* are ``(id, name)`` pairs and *places* town and country names, both
    already limited to what this viewer can see. The ``*_set`` flags say what
    the page has already chosen, which the phrase then leaves alone.
    """
    if not text or not text.strip():
        return Understood(text=text or "")
    words = _WORD.findall(text.replace("’", "'"))
    if not words:
        return Understood(text=text)
    claimed = [False] * len(words)
    result = Understood(text=text)

    if not person_set:
        # Longest names first, so "Maya Raj" is one person and not "Maya".
        spellings = _names(people)
        for key in sorted(spellings, key=lambda k: (-len(k.split(" ")), -len(k))):
            pid, name = spellings[key]
            if any(p == pid for p, _ in result.people):
                continue
            if _take(words, claimed, key):
                result.people.append((pid, name))

    known = {}
    for place in places:
        place = (place or "").strip()
        if len(place) >= 3:
            known.setdefault(_norm(place), place)
    for key in sorted(known, key=lambda k: (-len(k.split(" ")), -len(k))):
        if _take(words, claimed, key):
            result.place = known[key]
            break

    if not kind_set:
        for i, word in enumerate(words):
            if not claimed[i] and word.casefold() in KIND_WORDS:
                result.kind = KIND_WORDS[word.casefold()]
                claimed[i] = True
                break
        # "live photos" is its own view; leave the phrase to the search.
    if not favorites_set:
        for i, word in enumerate(words):
            if not claimed[i] and word.casefold() in _FAVOURITE_WORDS:
                result.favorites = True
                claimed[i] = True
                break

    if not result.matched:
        return result

    left = [w for w, used in zip(words, claimed) if not used]
    while left and left[0].casefold() in _JOINERS:
        left.pop(0)
    while left and left[-1].casefold() in _JOINERS:
        left.pop()
    if all(w.casefold() in _JOINERS for w in left):
        left = []
    result.text = " ".join(left)
    return result
