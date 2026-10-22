"""Young faces, older names: who an unnamed child in an old photograph may be.

The family has named Paati in hundreds of photographs from the 1980s on, and
there is a 1962 print of a girl of eight that nobody has named. The ordinary
matcher (:mod:`ninaivu.media.facematch`) compares that girl with Paati's
*average* face, which is a woman of fifty, and says nothing. This module asks
the question the family would ask instead: does she look like Paati did in
her *earliest* named photographs, could Paati have been in a photograph from
that year at all, and is anybody beside her somebody Paati grew up with?

So each named person is described by their youngest faces, not their middle:
the confirmed faces from the first :data:`YOUNG_SPAN` years of their named
photographs. A face from earlier than that is scored against those, and the
further back it is, the more the face may have changed, so the more it is
allowed to differ (:data:`PER_YEAR`, up to :data:`MAX_ALLOWANCE`). Whatever
the allowance, nothing below the recogniser's own same-person line
(``Thresholds.same``) is ever offered, as nowhere else in Ninaivu is: the
allowance only lowers the extra margin these suggestions start with
(:data:`BASE`), because without a gap of years the ordinary review queue has
already asked.

The family tree and the year somebody was born (the same year Then and now
and A life in photographs keep) narrow it further. Nobody is suggested for a
photograph taken before they were born, nor for one where they are already
named. A parent, grandparent, brother, sister or husband or wife named in the
same photograph is a reason to look harder (:data:`FAMILY_ALLOWANCE`), and is
said as the reason. When two people fit nearly as well (sisters, often) both
are offered, and a person answers.

Everything here works on vectors and plain values; the caller reads the
index with the viewer's own limits (api/api_young_faces.py). Nothing is
written: a suggestion becomes a name only when a member of the family says
yes, or a relative answers a "Who is this?" link. Every number below is a
guess, chosen inside the empty band between same and different people that
facematch describes, and not yet tried on the family's own photographs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

from . import facematch

__all__ = [
    "YOUNG_SPAN", "MIN_DATED", "MIN_GAP", "BASE", "PER_YEAR", "MAX_ALLOWANCE",
    "UNDATED_ALLOWANCE", "FAMILY_ALLOWANCE", "CLOSE_RELATIONS", "TRUSTED_DATES",
    "Known", "Unnamed", "Reference", "references", "year_of", "cutoff", "near", "suggest",
]

#: A person's "young faces" are the confirmed ones from this many years after
#: their earliest dated named photograph.
YOUNG_SPAN = 8
#: Fewest dated confirmed faces a person needs before anybody is suggested as
#: them: one or two named photographs say too little about how they looked.
MIN_DATED = 3
#: Fewest young faces kept; the earliest dated faces when the span holds fewer.
MIN_YOUNG = 3
#: A face must be at least this many years older than the person's earliest
#: named photograph. Closer than that, the ordinary review queue asks.
MIN_GAP = 2
#: The extra similarity, above the recogniser's same-person line, that a
#: suggestion starts out needing.
BASE = 0.10
#: How much of that extra each year between the photograph and the person's
#: earliest named one takes away, and the most it may take: twenty years back
#: needs only the same-person line itself.
PER_YEAR = 0.005
MAX_ALLOWANCE = 0.10
#: For a photograph whose year is not known (a print scanned with no date,
#: a file whose only date is when it was copied): some allowance, since an
#: undated print is usually an old one, but less than a known old year earns.
UNDATED_ALLOWANCE = 0.04
#: For a close relation of the person named in the same photograph.
FAMILY_ALLOWANCE = 0.03
#: The relations that make a childhood photograph likelier: the people a child
#: is photographed with. A son or daughter beside them says the opposite.
CLOSE_RELATIONS = ("parent", "grandparent", "sibling", "spouse")
#: Where a photograph's date comes from when it can be believed. A file's own
#: modified or created time is when it was copied or scanned, not taken.
TRUSTED_DATES = ("exif", "container", "manual", "path", "filename")
#: Most suggestions kept for one person, and in all.
PER_PERSON = 40
MOST = 400


def year_of(date_key: Any, date_source: Any) -> int:
    """The year a photograph was taken, or 0 when its date cannot be believed."""
    if str(date_source or "") not in TRUSTED_DATES:
        return 0
    try:
        year = int(str(date_key or "")[:4])
    except ValueError:
        return 0
    return year if 1826 <= year <= 2200 else 0


@dataclass
class Known:
    """A named person, as their confirmed faces describe them."""

    person_id: int
    vectors: list = field(default_factory=list)
    years: list[int] = field(default_factory=list)   # 0 where not known
    face_ids: list[int] = field(default_factory=list)
    born: int | None = None


@dataclass
class Unnamed:
    """One face nobody has named yet."""

    face_id: int
    asset_id: int
    vector: Any
    year: int = 0
    quality: float = 0.0


@dataclass
class Reference:
    """A person's youngest named faces, which a face from before is held to."""

    person_id: int
    earliest: int                  # the year of their earliest dated named photograph
    centroid: Any
    exemplars: list
    face_id: int                   # the earliest named face, to show beside a suggestion
    born: int | None = None
    young: int = 0                 # how many faces describe them young


def references(known: Iterable[Known]) -> list[Reference]:
    """Each person's youngest faces, for everybody with enough dated ones."""
    out: list[Reference] = []
    if np is None:
        return out
    for person in known:
        dated = sorted((y, n) for n, y in enumerate(person.years) if y)
        if len(dated) < MIN_DATED:
            continue
        earliest = dated[0][0]
        young = [n for y, n in dated if y <= earliest + YOUNG_SPAN]
        if len(young) < MIN_YOUNG:
            young = [n for _, n in dated[:MIN_YOUNG]]
        vectors = [facematch.unit(person.vectors[n]) for n in young]
        centre = facematch.centroid(vectors)
        if centre is None:
            continue
        exemplars: list = []
        for vector in vectors:
            facematch._update_exemplars(exemplars, vector)        # noqa: SLF001
        out.append(Reference(person.person_id, earliest, centre, exemplars,
                             int(person.face_ids[dated[0][1]]), person.born, len(young)))
    return out


def cutoff(refs: Sequence[Reference]) -> int:
    """The newest year a dated face can be from and still be suggested as
    anybody: older than somebody's earliest named photograph by MIN_GAP."""
    return max((r.earliest for r in refs), default=0) - MIN_GAP


def _scores(vectors: "np.ndarray", refs: Sequence[Reference]) -> "np.ndarray":
    """How like each person's young faces each face is, (faces x people).

    Half the young centroid, half the closest young face: the centroid is
    steady, the closest face is what a girl of eight resembles when the
    person's earliest named photographs run from eight to sixteen.
    """
    centres = np.vstack([r.centroid for r in refs]).astype("float32")
    near = vectors @ centres.T
    stack = np.vstack([e for r in refs for e in r.exemplars]).astype("float32")
    starts = np.cumsum([0] + [len(r.exemplars) for r in refs[:-1]])
    best = np.maximum.reduceat(vectors @ stack.T, starts, axis=1)
    return 0.5 * (near + best)


def near(vectors: "np.ndarray", refs: Sequence[Reference],
         rules: facematch.Thresholds = facematch.SFACE) -> "np.ndarray":
    """Which rows of *vectors* (unit, one face a row) clear the same-person
    line for anybody's young faces: the only faces :func:`suggest` could offer,
    so a caller reading a library in pieces keeps just these."""
    if np is None or not len(vectors) or not refs:
        return np.zeros(len(vectors), dtype=bool) if np is not None else []
    return _scores(np.asarray(vectors, dtype="float32"), refs).max(axis=1) >= rules.same


def suggest(faces: Sequence[Unnamed], refs: Sequence[Reference], *,
            rules: facematch.Thresholds = facematch.SFACE,
            present: dict[int, set[int]] | None = None,
            blocked: Iterable[tuple[int, int]] = (),
            relation=None) -> list[dict[str, Any]]:  # noqa: ANN001
    """Who each face in *faces* may be, best first.

    *present* gives the people already named in each photograph (by asset
    id); *blocked* the ``(face, person)`` pairs somebody has said no to;
    *relation* ``(person, other) -> str`` says what *other* is to *person* on
    the family tree ('' for nothing). One suggestion per face, and per
    photograph and person only the likeliest face. Each is a dict: ``face_id``,
    ``asset_id``, ``person_id``, ``score``, ``year`` (0 not known), ``age``
    (None not known), ``gap`` (years before the earliest named photograph),
    ``family`` (``[{"id", "relation"}]`` named beside them) and ``also`` (a
    second person who fits nearly as well, or None).
    """
    if np is None or not faces or not refs:
        return []
    present = present or {}
    no = {(int(f), int(p)) for f, p in blocked}
    vectors = np.vstack([facematch.unit(f.vector) for f in faces]).astype("float32")
    scores = _scores(vectors, refs)
    lowest = rules.same
    found: dict[tuple[int, int], dict[str, Any]] = {}
    for row, face in enumerate(faces):
        line = scores[row]
        if float(line.max()) < lowest:
            continue
        fits: list[tuple[float, float, dict[str, Any]]] = []
        beside = present.get(int(face.asset_id), set())
        for column in np.flatnonzero(line >= lowest):
            ref = refs[int(column)]
            pid = int(ref.person_id)
            if pid in beside or (int(face.face_id), pid) in no:
                continue
            if face.year:
                gap = ref.earliest - face.year
                if gap < MIN_GAP:
                    continue
                if ref.born and face.year < ref.born:
                    continue
                allowance = min(MAX_ALLOWANCE, PER_YEAR * gap)
            else:
                gap = 0
                allowance = UNDATED_ALLOWANCE
            family = []
            if relation is not None:
                for other in sorted(beside):
                    said = relation(pid, other)
                    if said in CLOSE_RELATIONS:
                        family.append({"id": int(other), "relation": said})
            if family:
                allowance += FAMILY_ALLOWANCE
            floor = max(rules.same, rules.same + BASE - allowance)
            score = float(line[column])
            if score < floor:
                continue
            age = face.year - ref.born if face.year and ref.born else None
            fits.append((score, score - floor, {
                "face_id": int(face.face_id), "asset_id": int(face.asset_id),
                "person_id": pid, "score": round(score, 4), "year": int(face.year),
                "age": age, "gap": int(gap), "family": family, "also": None}))
        if not fits:
            continue
        fits.sort(key=lambda f: (-f[0], f[2]["person_id"]))
        score, clear, best = fits[0]
        # Sisters, or a mother and her daughter at the same age: when the next
        # fits within the sibling margin, both are offered and a person says.
        if len(fits) > 1 and score - fits[1][0] < rules.margin:
            best["also"] = fits[1][2]["person_id"]
        best["clear"] = round(clear, 4)
        key = (best["asset_id"], best["person_id"])
        if key not in found or found[key]["score"] < best["score"]:
            found[key] = best
    ranked = sorted(found.values(), key=lambda s: (-s["clear"], -s["score"], s["face_id"]))
    per: dict[int, int] = {}
    out = []
    for item in ranked:
        n = per.get(item["person_id"], 0)
        if n >= PER_PERSON:
            continue
        per[item["person_id"]] = n + 1
        out.append(item)
        if len(out) >= MOST:
            break
    return out
