"""Reading a question: what is asked, about whom, where, when, and of what.

"When did we last go to Ooty with Paati?", "பாட்டியுடன் கடைசியாக ஊட்டிக்கு
எப்போது போனோம்?", "How many photos of Arjun from 2019?", "What is new this
week?". A question is the gallery's search phrase with a question around it,
so it is read the way the search box reads a phrase (utils/query.py for the
dates, utils/phrase.py for people, places and kinds), after three things
only a question has:

* **what is asked** (:data:`INTENTS`): how many, when, the first or the last
  time, where, who, what is new, or simply "show me";
* **the question's own words** ("when did we", "எப்போது போனோம்"), which
  are not what the photograph shows and are taken out before searching;
* **Tamil**: the question's language is the answer's; Tamil month and
  relative-date words are read as their English twins, and a name with a
  Tamil ending ("பாட்டியுடன்", with Paati) is the name.

Like the search box it only takes what it is sure of: a name is a person only
when it is one this viewer can already see, and anything not understood is
left in the words to search for. With the writing model downloaded, the
model may read the question as well (:func:`merge_plan`): it is good at
Tamil and at what a question means, and is held to the same names and
places, so it can add to what was understood and never invent a person.

Nothing here touches the database or the network.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from ..utils import phrase, query

#: What a question can ask, as the answer is shaped for it.
INTENTS = ("count", "when", "first", "last", "where", "who", "new", "show")

_TAMIL = re.compile(r"[஀-௿]")


def language_of(text: str) -> str:
    """``"ta"`` for a question with Tamil letters in it, else ``"en"``."""
    return "ta" if _TAMIL.search(text or "") else "en"


# -- what is asked --------------------------------------------------------------

#: Phrases that say what is asked, in the order they are tried: the more
#: particular first ("how many" before "when", "last time" before "when").
_ASKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("new", ("what's new", "what is new", "anything new", "new photos", "new videos",
             "recently added", "just added", "were added", "was added", "been added",
             "புதிதாக", "புதிய படங்கள்", "புதியவை", "சேர்க்கப்பட்ட", "சேர்த்த")),
    ("count", ("how many", "how much", "number of", "count of",
               "எத்தனை", "எண்ணிக்கை")),
    ("first", ("first time", "for the first time", "earliest", "first photo",
               "first picture", "முதல் முறை", "முதன்முறை", "முதல்முறையாக", "முதன் முதலில்",
               "முதலில்", "முதல் படம்")),
    ("last", ("last time", "most recent", "latest", "the last", "did we last", "did i last",
              "we last", "i last", "recently", "கடைசியாக", "கடைசி முறை", "சமீபத்தில்",
              "சமீபத்திய")),
    ("when", ("when", "what year", "which year", "what date", "which day", "what month",
              "எப்போது", "எப்பொழுது", "எந்த ஆண்டு", "எந்த வருடம்", "எந்த வருஷம்",
              "எந்த நாள்", "எந்த மாதம்")),
    ("where", ("where", "which place", "which places", "what place", "which city",
               "எங்கே", "எங்கு", "எந்த இடம்", "எந்த ஊர்", "எந்த ஊரில்")),
    ("who", ("who", "with whom", "whom", "யார்", "யாருடன்", "யாரெல்லாம்", "யாரை")),
)

#: Words of the question itself, in either language, that are not what a
#: photograph is of. Taken out of what is searched for.
_QUESTION_WORDS = frozenset("""
    what what's whats when where who whom whose which why how many much
    is are was were be been being am do does did done have has had
    we our ours us i me my mine you your they their them he she his her it its
    go went gone going visit visited visiting see saw seen take took taken
    time times last first latest earliest recent recently most ever all any
    show find give tell list let get got there here this that these those
    can could would should please some number count of the a an to in on at
    with from for and or by about photo photos picture pictures pic pics
    photograph photographs taken new added just
    எப்போது எப்பொழுது எங்கே எங்கு யார் யாருடன் யாரெல்லாம் யாரை எத்தனை எண்ணிக்கை
    என்ன எந்த ஆண்டு வருடம் வருஷம் நாள் மாதம் இடம் ஊர் ஊரில் முறை
    நாம் நாங்கள் நாங்க நான் எங்கள் எங்க நம் நம்ம என் எனது
    போனோம் போனேன் சென்றோம் சென்றேன் போயிருந்தோம் வந்தோம் வந்தார் வந்தாள் வந்தார்கள்
    பார்த்தோம் இருந்தது இருந்தோம் இருந்தார் இருந்தாள் உள்ள உள்ளன இருக்கும் இருக்கிறது
    கடைசியாக கடைசி முதல் முதலில் முதன்முறை முதல்முறையாக சமீபத்தில் சமீபத்திய
    படம் படங்கள் படங்களை படத்தில் புகைப்படம் புகைப்படங்கள் புகைப்படங்களை
    காட்டு காட்டுங்கள் காண்பி சொல் சொல்லுங்கள் தேடு உடன் மற்றும் அல்லது
    புதிய புதிதாக புதியவை சேர்க்கப்பட்ட சேர்த்த அனைத்து எல்லா
""".split())

#: Tamil date words, said again in English for the date reader.
_TAMIL_DATES: tuple[tuple[str, str], ...] = (
    ("இந்த வருடம்", "this year"), ("இந்த ஆண்டு", "this year"), ("இந்த வருஷம்", "this year"),
    ("போன வருடம்", "last year"), ("போன ஆண்டு", "last year"), ("கடந்த ஆண்டு", "last year"),
    ("கடந்த வருடம்", "last year"), ("போன வருஷம்", "last year"),
    ("இந்த மாதம்", "this month"), ("போன மாதம்", "last month"), ("கடந்த மாதம்", "last month"),
    ("இந்த வாரம்", "this week"), ("போன வாரம்", "last week"), ("கடந்த வாரம்", "last week"),
    ("நேற்று", "yesterday"), ("இன்று", "today"),
    ("ஜனவரி", "january"), ("பிப்ரவரி", "february"), ("மார்ச்", "march"),
    ("ஏப்ரல்", "april"), ("மே", "in may"), ("ஜூன்", "june"), ("ஜூலை", "july"),
    ("ஆகஸ்ட்", "august"), ("செப்டம்பர்", "september"), ("அக்டோபர்", "october"),
    ("நவம்பர்", "november"), ("டிசம்பர்", "december"),
)

#: Endings Tamil puts on a name: with, of, to, in, from, and the like. A word
#: is a known name when it is the name followed by one of these.
_TAMIL_ENDINGS = ("உடன்", "ுடன்", "டன்", "ோடு", "வுடன்", "ின்", "இன்", "ன்", "ை", "ஐ",
                  "க்கு", "ுக்கு", "ிற்கு", "ில்", "இல்", "ல்", "ிலிருந்து", "லிருந்து",
                  "ும்", "உம்", "ா", "யின்", "வின்", "ுடைய", "ுக்", "க்")


@dataclass
class Question:
    """What a question was read as."""

    asked: str
    language: str = "en"
    intent: str = "show"
    text: str = ""
    people: list[tuple[int, str]] = field(default_factory=list)
    place: str = ""
    kind: str = ""
    favorites: bool = False
    window: query.Window | None = None
    #: Read by the writing model as well as by the rules.
    planned: bool = False

    def chips(self) -> list[dict[str, str]]:
        """The understood parts, for the page to show as the search box does."""
        out = [{"type": "person", "label": name, "id": str(pid)} for pid, name in self.people]
        if self.place:
            out.append({"type": "place", "label": self.place})
        if self.window is not None:
            out.append({"type": "dates", "label": self.window.label,
                        "from": self.window.date_from, "to": self.window.date_to})
        if self.kind:
            out.append({"type": "kind", "label": "Videos" if self.kind == "video" else "Photos"})
        if self.favorites:
            out.append({"type": "favorites", "label": "Favourites"})
        if self.text:
            out.append({"type": "words", "label": self.text})
        return out


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def intent_of(text: str) -> str:
    """What the question asks (:data:`INTENTS`)."""
    lowered = " " + _norm(text).casefold() + " "
    for intent, phrases in _ASKS:
        for said in phrases:
            if _TAMIL.search(said):
                if said in lowered:
                    return intent
            elif re.search(rf"(?<![\w']){re.escape(said)}(?![\w'])", lowered):
                return intent
    return "show"


def _tamil_dates(text: str) -> str:
    """Tamil date words in English; a year glued to its Tamil ending
    ("2019ல்", in 2019) parted from it."""
    text = re.sub(r"((?:19|20)\d{2})(?=[஀-௿])", r"\1 ", text)
    for tamil, english in _TAMIL_DATES:
        text = re.sub(rf"(?<![஀-௿]){re.escape(tamil)}[஀-௿]*",
                      f" {english} ", text)
    return re.sub(r"\s+", " ", text).strip()


def _words(text: str) -> list[str]:
    """Words, Tamil ones whole with their vowel signs."""
    return re.findall(r"[\w஀-௿'&+-]+", text)


def _name_in(word: str, name: str) -> bool:
    """Whether *word* is *name*, or *name* with a Tamil ending."""
    if word == name:
        return True
    if not word.startswith(name) or not _TAMIL.search(name) or len(name) < 3:
        return False
    ending = word[len(name):]
    # The endings Tamil writes on a name are short, and joined to it with
    # the letter its last sound calls for (பாட்டி-யுடன், ஊட்டி-க்கு).
    return ending in _TAMIL_ENDINGS or (len(ending) <= 7 and all(
        "\u0B80" <= ch <= "\u0BFF" for ch in ending))


def _tamil_mentions(text: str, people: Iterable[tuple[int, str]],
                    places: Iterable[str]) -> tuple[list[tuple[int, str]], str, set[str]]:
    """People and a place named in Tamil in *text*, with their endings, and
    the words that named them. A name in Latin letters is the phrase
    reader's (utils/phrase.py)."""
    words = _words(text)
    found_people: list[tuple[int, str]] = []
    used: set[str] = set()
    for pid, name in people:
        name = _norm(name)
        if not _TAMIL.search(name) or len(name) < 2:
            continue
        parts = name.split(" ")
        for i in range(len(words) - len(parts) + 1):
            window = words[i:i + len(parts)]
            if window[:-1] == parts[:-1] and _name_in(window[-1], parts[-1]):
                if (pid, name) not in found_people:
                    found_people.append((int(pid), name))
                used.update(window)
                break
    place = ""
    for name in sorted({_norm(p) for p in places if p}, key=len, reverse=True):
        if not _TAMIL.search(name):
            continue
        for word in words:
            if _name_in(word, name):
                place = name
                used.add(word)
                break
        if place:
            break
    return found_people, place, used


def _leftover(text: str, drop: set[str] = frozenset()) -> str:
    """What is left to search for once the question's own words are out."""
    kept = [w for w in _words(text)
            if w.casefold() not in _QUESTION_WORDS and w not in drop
            and not re.fullmatch(r"\d{1,2}", w)
            # An ending left behind when its year or name was read.
            and not (w in _TAMIL_ENDINGS or (_TAMIL.match(w) and len(w) <= 2))]
    return " ".join(kept).strip(" -'")


def understand(asked: str, *, people: Iterable[tuple[int, str]] = (),
               places: Iterable[str] = (), today: date | None = None,
               southern: bool = False) -> Question:
    """Read *asked*. *people* and *places* are the ones this viewer can see."""
    people = list(people)
    places = list(places)
    asked = _norm(asked)[:500]
    q = Question(asked=asked, language=language_of(asked), intent=intent_of(asked))
    text = asked.rstrip("?？!. ")
    if q.language == "ta":
        text = _tamil_dates(text)
    remaining, window = query.parse(text, today=today, southern=southern)
    q.window = window
    tamil_people, tamil_place, used = _tamil_mentions(remaining, people, places)
    read = phrase.parse(remaining, people=people, places=places)
    q.people = list(dict.fromkeys([*read.people, *tamil_people]))
    q.place = read.place or tamil_place
    q.kind = read.kind
    q.favorites = read.favorites
    q.text = _leftover(read.text if read.matched else remaining, used)
    return q


# -- the writing model's reading ----------------------------------------------

#: What the writing model is asked to fill in when it reads a question. Held
#: to this shape by llama.cpp's grammar (runtime/llm.py).
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "search": {"type": "string"},
        "people": {"type": "array", "items": {"type": "string"}},
        "place": {"type": "string"},
        "from": {"type": "string"},
        "to": {"type": "string"},
    },
    "required": ["intent", "search", "people", "place", "from", "to"],
}


def plan_messages(q: Question, people: Iterable[tuple[int, str]],
                  places: Iterable[str], today: date | None = None) -> list[dict[str, str]]:
    """The writing model's instructions for reading *q*."""
    today = today or date.today()
    names = [name for _, name in people][:300]
    towns = list(places)[:300]
    system = (
        "You read questions about a family's own photo library and turn them into a "
        "search. Reply with JSON only. Fields: intent (one of count, when, first, last, "
        "where, who, new, show); search (a few English words for what is in the photos, "
        "e.g. 'beach', 'birthday cake', 'temple'; empty if none); people (names exactly as "
        "written in the list of people, only those the question means); place (one place "
        "exactly as written in the list of places, or empty); from and to (dates as "
        "YYYY-MM-DD when the question names a time, else empty). Never invent names or "
        f"places. Today is {today.isoformat()}.")
    user = (f"People: {', '.join(names) or '(none)'}\n"
            f"Places: {', '.join(towns) or '(none)'}\n"
            f"Question: {q.asked}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def merge_plan(q: Question, plan: Any, *, people: Iterable[tuple[int, str]] = (),
               places: Iterable[str] = ()) -> Question:
    """Add what the writing model read in *q* to what the rules did.

    The rules win where both found something; the model's people and place
    count only when they are, word for word, ones this viewer can see; its
    dates only when they make a range. A plan that is not the right shape
    changes nothing.
    """
    if not isinstance(plan, dict):
        return q
    by_name = {_norm(name).casefold(): (int(pid), name) for pid, name in people}
    towns = {_norm(p).casefold(): p for p in places if p}
    if q.intent == "show" and plan.get("intent") in INTENTS:
        q.intent = str(plan["intent"])
    if not q.people:
        for name in plan.get("people") or []:
            hit = by_name.get(_norm(str(name)).casefold())
            if hit and hit not in q.people:
                q.people.append(hit)
    if not q.place:
        q.place = towns.get(_norm(str(plan.get("place") or "")).casefold(), "")
    if q.window is None:
        start, end = _date(plan.get("from")), _date(plan.get("to"))
        if start and not end:
            end = start
        if end and not start:
            start = end
        if start and end and start <= end and start.year >= 1826:
            q.window = query.Window(start=start, end=end,
                                    label=f"{start.isoformat()} – {end.isoformat()}")
    search = _norm(str(plan.get("search") or ""))[:80]
    if _TAMIL.search(search):
        search = ""
    if q.language == "ta":
        # The model's English words find what a Tamil question describes, and
        # its reading of the names and places replaces the Tamil words the
        # rules could not place (பாட்டியுடன் is a person, not a word to search).
        q.text = _leftover(search)
    elif search and not q.text:
        q.text = _leftover(search)
    q.planned = True
    return q
