"""Deciding who a face belongs to.

:mod:`ninaivu.faces` turns pixels into 128-value embeddings. Everything in this
module works on those embeddings alone — no OpenCV, no models, no database —
so the rules that decide identity can be tested exhaustively on synthetic
vectors, which is the only way to be confident about them. The engine is the
part that needs a model on disk; the policy is the part that needs to be right.

Two similarity numbers matter, both cosine over L2-normalised vectors:

Measured on real photographs with this model, two frames of the same person
sit at 0.77, and the same face survives heavy JPEG, blur, greyscale, a 20°
tilt and a drop to 52 pixels while staying above 0.67. Two different people
sit at 0.04–0.18. OpenCV publishes 0.363 as SFace's same-identity threshold.
So there is a wide empty band between "certainly the same" and "certainly
not", and the thresholds below are placed inside it deliberately far from
both edges.

The policy that follows is asymmetric on purpose. A wrong auto-assignment is
expensive: it puts a stranger in a family member's album, and nobody goes
looking for it. A missed assignment is cheap: the face lands in the review
queue and one click fixes it. So every threshold is set where a mistake
becomes a question rather than an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


# --- thresholds ------------------------------------------------------------

#: OpenCV's published same-identity threshold for SFace. Nothing below this is
#: ever shown as a possible match, in any part of the interface.
COSINE_SAME = 0.363

#: Assign a face to a named person without asking. Well above the highest
#: different-person similarity measured, and well below the lowest
#: same-person similarity measured under degradation.
T_AUTO = 0.50

#: Put a face in the review queue rather than dropping it.
T_SUGGEST = COSINE_SAME

#: How far ahead of the runner-up person a match must be to be automatic.
#: This is the guard that stops siblings and parent/child pairs being merged:
#: they are the one case where an absolute threshold is not enough, because
#: the face genuinely does resemble two different people in the library.
MARGIN = 0.06

#: Join an existing unnamed cluster (similarity to its centroid).
T_CLUSTER = 0.46

#: Minimum similarity to *every* exemplar of a cluster before joining it.
#: Without this a chain of individually-plausible links walks a cluster across
#: several identities — A resembles B, B resembles C, and C is not A. This is
#: the single most important rule here.
T_LINK = 0.38

#: How many members stand in for a cluster in the link check. They are kept
#: deliberately spread out, so they describe the cluster's extent rather than
#: its middle.
MAX_EXEMPLARS = 16

#: A cluster smaller than this is not offered for naming — it is usually one
#: bad crop, and a console full of them is a console nobody uses.
MIN_CLUSTER_SIZE = 3


@dataclass
class Candidate:
    """One face, as the matcher sees it."""

    face_id: int
    vector: "np.ndarray"
    quality: float = 0.0
    asset_id: int = 0


@dataclass
class Cluster:
    """A group the matcher believes is one person, before anyone names them."""

    members: list[int] = field(default_factory=list)
    centroid: "np.ndarray | None" = None
    exemplars: list["np.ndarray"] = field(default_factory=list)
    _sum: "np.ndarray | None" = None

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class Person:
    """A named person, described by the faces an admin has confirmed."""

    person_id: int
    centroid: "np.ndarray"
    exemplars: list["np.ndarray"] = field(default_factory=list)
    name: str = ""


@dataclass
class Assignment:
    """What the matcher concluded about one face."""

    person_id: int | None
    score: float
    decision: str            # 'auto' | 'suggest' | 'none'
    runner_up: int | None = None
    runner_up_score: float = 0.0

    @property
    def confident(self) -> bool:
        return self.decision == "auto"


# --- vector helpers --------------------------------------------------------

def unit(vector) -> "np.ndarray":
    vec = np.asarray(vector, dtype="float32").reshape(-1)
    norm = float(np.linalg.norm(vec))
    return vec if norm == 0 else (vec / norm).astype("float32")


def similarity(a, b) -> float:
    """Cosine similarity between two embeddings."""
    return float(np.dot(unit(a), unit(b)))


def centroid(vectors: Sequence) -> "np.ndarray | None":
    """The direction a set of faces points in.

    A mean of unit vectors, re-normalised. Re-normalising matters: without it
    a person with many similar faces produces a long vector and a person with
    few produces a short one, and every comparison afterwards is biased toward
    whoever has the most photographs in the library.
    """
    if np is None or not len(vectors):
        return None
    stack = np.vstack([unit(v) for v in vectors]).astype("float32")
    mean = stack.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if not norm:
        return None
    return (mean / norm).astype("float32")


def _update_exemplars(exemplars: list, vector) -> None:
    """Keep up to :data:`MAX_EXEMPLARS` members, as spread out as possible.

    The link check is only as good as what it checks against. Exemplars chosen
    at random describe the cluster's middle; these are chosen so that the
    least-similar members survive, which is what makes the check able to say
    "this face matches the average of the cluster but not this corner of it".
    """
    if len(exemplars) < MAX_EXEMPLARS:
        exemplars.append(vector)
        return
    matrix = np.vstack(exemplars)
    # How tightly each current exemplar is already covered by the others.
    sims = matrix @ matrix.T
    np.fill_diagonal(sims, -1.0)
    coverage = sims.max(axis=1)
    most_redundant = int(coverage.argmax())
    # Swap only if the newcomer is further from the set than the most
    # redundant member is, i.e. only if it widens what the set describes.
    newcomer_coverage = float((matrix @ vector).max())
    if newcomer_coverage < float(coverage[most_redundant]):
        exemplars[most_redundant] = vector


def _add(cluster: Cluster, candidate: Candidate) -> None:
    cluster.members.append(candidate.face_id)
    vector = unit(candidate.vector)
    cluster._sum = vector.copy() if cluster._sum is None else cluster._sum + vector
    norm = float(np.linalg.norm(cluster._sum))
    cluster.centroid = (cluster._sum / norm).astype("float32") if norm else vector
    _update_exemplars(cluster.exemplars, vector)


# --- clustering ------------------------------------------------------------

def cluster_faces(
    candidates: Sequence[Candidate],
    *,
    centroid_min: float = T_CLUSTER,
    link_min: float = T_LINK,
) -> list[Cluster]:
    """Group unassigned faces into probable people.

    Greedy agglomeration in descending quality order, so every cluster forms
    around its sharpest, largest faces and the marginal crops join something
    that is already well defined — rather than two blurry faces founding a
    cluster that then attracts everything vaguely like either of them.

    A face joins the *best-scoring* cluster it fits, not the first, and it
    must pass both the centroid test and the exemplar link test to fit at all.
    Faces matching nothing start their own cluster.
    """
    if np is None:
        return []
    ordered = sorted(candidates, key=lambda c: (-c.quality, c.face_id))
    clusters: list[Cluster] = []
    # Every cluster's centroid as one row, so a face is scored against all of
    # them in a single product. Asking each cluster in turn was a Python call
    # per face per cluster: most faces found nothing and founded a cluster of
    # their own, so the work grew with the square of the library — minutes on
    # a large one, and naming any group in the console regroups everything.
    centres: "np.ndarray | None" = None

    for candidate in ordered:
        vector = unit(candidate.vector)
        best: int | None = None
        if clusters:
            scores = centres[:len(clusters)] @ vector
            near = np.flatnonzero(scores >= centroid_min)
            # Best centroid first; the first that also passes the link test is
            # the best one that fits. A stable sort keeps the earlier cluster
            # on a tie, as the loop it replaced did.
            for index in near[np.argsort(-scores[near], kind="stable")]:
                exemplars = clusters[index].exemplars
                if exemplars and float((np.vstack(exemplars) @ vector).min()) < link_min:
                    continue
                best = int(index)
                break
        if best is None:
            best = len(clusters)
            clusters.append(Cluster())
            if centres is None:
                centres = np.empty((1024, vector.shape[0]), dtype="float32")
            elif best == len(centres):
                centres = np.vstack([centres, np.empty_like(centres)])
        _add(clusters[best], candidate)
        centres[best] = clusters[best].centroid

    clusters.sort(key=lambda c: -c.size)
    return clusters


def merge_candidates(clusters: Sequence[Cluster],
                     *, threshold: float = 0.62) -> list[tuple[int, int, float]]:
    """Pairs of clusters that are probably the same person.

    Deliberately *not* applied automatically. Two clusters of the same person
    usually differ by something systematic — a decade, a beard, glasses — and
    the similarity that separates "same person, different era" from "brother"
    is not reliable enough to act on unasked. So this produces a list the
    console offers as "are these the same person?" and nothing more.
    """
    if np is None or len(clusters) < 2:
        return []
    pairs: list[tuple[int, int, float]] = []
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            a, b = clusters[i], clusters[j]
            if a.centroid is None or b.centroid is None:
                continue
            score = float(np.dot(a.centroid, b.centroid))
            if score >= threshold:
                pairs.append((i, j, score))
    pairs.sort(key=lambda p: -p[2])
    return pairs


# --- assignment to named people --------------------------------------------

def assign_face(
    vector,
    people: Sequence[Person],
    *,
    blocked: Iterable[int] = (),
    auto: float = T_AUTO,
    suggest: float = T_SUGGEST,
    margin: float = MARGIN,
) -> Assignment:
    """Decide whether one face belongs to one of the named people.

    Three things must all hold before a face is assigned without asking:

    1. it clears :data:`T_AUTO` against that person's centroid;
    2. it beats the runner-up person by :data:`MARGIN` — the sibling guard;
    3. it clears :data:`T_LINK` against at least one face actually confirmed
       for that person, not merely against their average.

    Failing any of those but clearing :data:`T_SUGGEST` makes it a question
    for the review queue. ``blocked`` carries the people this face has already
    been rejected for, and they are removed from consideration entirely — a
    rejection is permanent, or the queue asks the same question forever.
    """
    if np is None or not people:
        return Assignment(None, 0.0, "none")

    vec = unit(vector)
    blocked_ids = set(int(b) for b in blocked)
    scored: list[tuple[float, Person]] = []
    for person in people:
        if int(person.person_id) in blocked_ids:
            continue
        if person.centroid is None:
            continue
        scored.append((float(np.dot(unit(person.centroid), vec)), person))

    if not scored:
        return Assignment(None, 0.0, "none")

    scored.sort(key=lambda pair: -pair[0])
    top_score, top = scored[0]
    runner_id, runner_score = (None, 0.0)
    if len(scored) > 1:
        runner_score, runner = scored[1]
        runner_id = int(runner.person_id)

    if top_score < suggest:
        return Assignment(None, top_score, "none",
                          runner_up=runner_id, runner_up_score=runner_score)

    clears_margin = (top_score - runner_score) >= margin if runner_id else True
    linked = True
    if top.exemplars:
        linked = float((np.vstack([unit(e) for e in top.exemplars]) @ vec).max()) >= T_LINK

    decision = "auto" if (top_score >= auto and clears_margin and linked) else "suggest"

    # A face somebody has already rejected for *anybody* is never assigned
    # automatically again. The rejection is evidence that this face sits in
    # the ambiguous zone — it looked enough like that person to be offered —
    # and the runner-up in an ambiguous zone is very often the lookalike the
    # first answer was distinguishing it from. So it goes back to a human.
    if blocked_ids and decision == "auto":
        decision = "suggest"

    return Assignment(int(top.person_id), top_score, decision,
                      runner_up=runner_id, runner_up_score=runner_score)


def rank_for_person(person: Person, candidates: Sequence[Candidate],
                    *, floor: float = T_SUGGEST,
                    blocked: Iterable[tuple[int, int]] = ()) -> list[tuple[int, float]]:
    """Faces most likely to be this person, best first — the review queue.

    Ordered by similarity so the admin confirms the obvious ones first; each
    confirmation sharpens the centroid, which re-ranks what is left. In
    practice that means the queue gets easier as it is worked through rather
    than harder.
    """
    if np is None or person.centroid is None or not candidates:
        return []
    blocked_pairs = {(int(f), int(p)) for f, p in blocked}
    centre = unit(person.centroid)
    out: list[tuple[int, float]] = []
    for candidate in candidates:
        if (int(candidate.face_id), int(person.person_id)) in blocked_pairs:
            continue
        score = float(np.dot(centre, unit(candidate.vector)))
        if score >= floor:
            out.append((int(candidate.face_id), score))
    out.sort(key=lambda pair: -pair[1])
    return out
