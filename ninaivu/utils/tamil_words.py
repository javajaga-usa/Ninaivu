"""Tamil in the search box: the family's names for each other, and the words
for what a photograph shows.

The search box already reads "Amma at the temple, 2019" (utils/query.py for
the year, utils/phrase.py for Amma). Typed in Tamil, the same search —
"அம்மா கோவிலில், 2019ல்" — found nothing it understood: the year had its
ending glued on, "அம்மா" was not the person the family had named *Amma* in
Latin letters, and "கோவிலில்" (at the temple) went to the picture search as
a word with its ending, which the picture model reads far worse than
"temple".

This holds the two small tables that close the gap, and nothing else:

* **What the family calls each other** (:data:`RELATIONS`): Amma, அம்மா, Mom
  and Mother are one person when the family named somebody by one of them,
  so a name typed in either script, or the English word, finds them. Only
  when exactly one person this viewer can see is named by a word of that
  group: two "Amma"s (a grandmother and a mother) are left to their names.
  "Mother" and "father" are not in the groups: they are as often what a
  photograph shows as who is in it.
* **What a photograph shows** (:data:`SCENES`): the Tamil words a family
  searches its photographs for — a temple, a wedding, the beach, Pongal —
  with the English the picture model was trained on. The Tamil word, its
  ending taken off, stays in the words searched for in captions, voice
  stories and descriptions; the English goes to the picture search only.

Nothing here touches the database or the network, and nothing is guessed:
a word in neither table is left as it was typed.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

__all__ = ["RELATIONS", "SCENES", "aliases", "for_pictures", "for_words", "has_tamil"]

_TAMIL = re.compile(r"[஀-௿]")

#: The vowel signs that take the place of a final pulli when an ending is
#: joined on (கோவில் + இல் is கோவிலில்).
_VOWEL_SIGNS = frozenset("ாிீுூெேைொோௌ")

#: Each group is one relation, every way a family writes it. The first word
#: of a group is only its label.
RELATIONS: tuple[tuple[str, ...], ...] = (
    ("amma", "அம்மா", "mom", "mum", "mummy"),
    ("appa", "அப்பா", "dad", "daddy"),
    ("paati", "patti", "paatti", "பாட்டி", "ஆயா", "grandma", "granny"),
    ("thatha", "thaatha", "தாத்தா", "grandpa", "grandad", "granddad"),
    ("akka", "அக்கா"),
    ("anna", "annan", "அண்ணா", "அண்ணன்"),
    ("thambi", "தம்பி"),
    ("thangai", "thangachi", "தங்கை", "தங்கச்சி"),
    ("mama", "maama", "மாமா"),
    ("mami", "maami", "மாமி"),
    ("athai", "aththai", "அத்தை"),
    ("chithi", "சித்தி"),
    ("chithappa", "சித்தப்பா"),
    ("periyamma", "பெரியம்மா"),
    ("periyappa", "பெரியப்பா"),
)

#: Tamil words for what is in a photograph, and the English the picture
#: search understands. Spelling variants are rows of their own.
SCENES: dict[str, str] = {
    "கோவில்": "temple", "கோயில்": "temple", "தேர்": "temple chariot",
    "கடற்கரை": "beach", "கடல்": "sea", "நதி": "river", "ஏரி": "lake",
    "அருவி": "waterfall", "மலை": "mountain", "பூங்கா": "park",
    "தோட்டம்": "garden", "வீடு": "house", "பள்ளி": "school",
    "மருத்துவமனை": "hospital", "திருமணம்": "wedding", "கல்யாணம்": "wedding",
    "நிச்சயதார்த்தம்": "engagement", "வளைகாப்பு": "baby shower",
    "பிறந்தநாள்": "birthday", "கேக்": "birthday cake", "பட்டமளிப்பு": "graduation",
    "திருவிழா": "festival", "பொங்கல்": "pongal festival", "தீபாவளி": "diwali",
    "பட்டாசு": "fireworks", "விளக்கு": "oil lamp", "கோலம்": "kolam",
    "மழை": "rain", "பனி": "snow", "பூக்கள்": "flowers", "பூ": "flower",
    "நாய்": "dog", "பூனை": "cat", "யானை": "elephant", "மாடு": "cow",
    "குழந்தை": "baby", "சாப்பாடு": "food", "உணவு": "food",
    "விமானம்": "airplane", "ரயில்": "train", "கார்": "car", "படகு": "boat",
    "நடனம்": "dance", "கிரிக்கெட்": "cricket", "சுற்றுலா": "trip",
    "புடவை": "saree", "இரவு": "night",
}

#: The endings a noun takes in a search: at, in, to, with, of, and.
_ENDINGS = ("இல்", "ில்", "யில்", "ல்", "இலே", "ிலே", "யிலே", "க்கு", "ுக்கு", "வுக்கு",
            "உக்கு", "ின்", "யின்", "இன்", "ும்", "யும்", "உம்", "ோடு", "உடன்", "ுடன்",
            "யுடன்", "வுடன்", "கள்", "க்கள்", "ிலிருந்து", "யிலிருந்து", "லிருந்து",
            "ை", "யை", "ஐ", "க்", "ப்", "த்")

#: What follows த்த on such a word: திருமண-த்த-ில், -ுக்கு, -ின், -ை.
_AFTER_TT = frozenset({"ில்", "ிலே", "ுக்கு", "ின்", "ை", "ோடு", "ுடன்", "ிலிருந்து", "ும்"})

_WORD = re.compile(r"[\w஀-௿'&+-]+")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text or "").strip().casefold()


def has_tamil(text: str) -> bool:
    return bool(_TAMIL.search(text or ""))


def aliases(people: Iterable[tuple[int, str]]) -> list[tuple[int, str]]:
    """Other spellings for the people named by a family word, as
    ``(id, spelling)`` pairs, for whoever reads names out of a phrase.

    Only when one person alone, of those this viewer can see, is named by a
    word of that relation: with two, a spelling would have to choose.
    """
    people = [(int(pid), _norm(name)) for pid, name in people if (name or "").strip()]
    found: list[tuple[int, str]] = []
    for group in RELATIONS:
        words = set(group)
        owners = {pid for pid, name in people if name in words}
        if len(owners) != 1:
            continue
        (pid,) = owners
        taken = {name for p, name in people if p == pid}
        # A spelling that is somebody else's whole name stays theirs.
        others = {name for p, name in people if p != pid}
        found.extend((pid, w) for w in group if w not in taken and w not in others)
    return found


def _scene(word: str) -> str | None:
    """The scene word *word* is, its ending taken off, or None."""
    word = unicodedata.normalize("NFC", word)
    if word in SCENES:
        return word
    for stem in sorted(SCENES, key=len, reverse=True):
        # A word ending in ம் takes its ending on த்த (திருமணம், திருமணத்தில்).
        if stem.endswith("ம்") and word.startswith(stem[:-2] + "த்த"):
            if word[len(stem) - 2 + 3:] in _AFTER_TT:
                return stem
        if not word.startswith(stem[:-1] if stem.endswith("்") else stem):
            continue
        if word.startswith(stem):
            rest = word[len(stem):]
        else:
            # A final pulli gives way to the vowel the ending starts with.
            rest = word[len(stem) - 1:]
            if not rest or rest[0] not in _VOWEL_SIGNS:
                continue
        if not rest:
            continue
        # Only a known ending: கார் (car) is also the start of கார்த்திக்.
        if rest in _ENDINGS:
            return stem
    return None


def _swap(text: str, to_english: bool) -> str:
    if not has_tamil(text):
        return text
    out: list[str] = []
    for word in _WORD.findall(text):
        stem = _scene(word) if _TAMIL.search(word) else None
        if stem is None:
            out.append(word)
        else:
            out.append(SCENES[stem] if to_english else stem)
    return " ".join(out)


def for_words(text: str) -> str:
    """*text* for the captions, voice stories and descriptions: each Tamil
    scene word without its ending (கோவிலில் → கோவில்)."""
    return _swap(text, False)


def for_pictures(text: str) -> str:
    """*text* for the picture search: each Tamil scene word in the English
    the picture model knows (கோவிலில் → temple)."""
    return _swap(text, True)
