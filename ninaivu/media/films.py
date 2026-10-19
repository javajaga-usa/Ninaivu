"""Memory films: a few minutes of video made from the family's own photographs,
clips and voice stories.

A book is something to hold; a film is something to sit in front of together.
This makes the film: forty or so of the best moments of a year, a trip or a
person, each photograph moving slowly towards the faces in it, the best few
seconds of each video clip, a grandparent's voice story told over the
photograph it is about with what they said written underneath in Tamil and
English, the family's own music under it all, ducked whenever somebody speaks,
and the cuts falling on the music's beat. Every frame is a real photograph or
a real clip: nothing is invented.

Three steps, like the books (media/books.py), and only the caller looks at the
library:

**Picking** is the books' own (:func:`ninaivu.media.books.pick`), done in the
request through the viewer's limits, so a film can hold nothing its maker
could not have scrolled to. The request hands this module a fixed list of
files and never asks the index what else there is.

**Planning** (:func:`plan`) is a pure function from that list to a timeline:
which shot starts when and lasts how long, which way each photograph moves,
where the narration and the clips' own sound go, when the music gives way, and
the subtitles. It is what the tests look at, and what a later step (Ninaivu
writing the storyboard itself, from what a family asks for) will produce.

**Rendering** (:func:`render`) draws every frame with Pillow, in sub-pixel
steps so a slow move does not judder, and pipes them to one ffmpeg encode;
the sound is mixed by ffmpeg beforehand. Tamil is set with the books' fonts
(raqm), so a Tamil title or subtitle prints the way it is written. It runs in
a process of its own at a low priority (:func:`start`), so the family's pages
come first while a film is made and stopping one is ending a process.

Nothing here talks to the network. The film is made on this computer and
stays in the state folder until its owner downloads or deletes it.
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from PIL import Image, ImageDraw, ImageFilter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shape of a film
# ---------------------------------------------------------------------------

FPS = 30
SIZES = {"720p": (1280, 720), "1080p": (1920, 1080)}
DEFAULT_SIZE = "1080p"
#: How many moments a film is made of: a two- to four-minute film.
DEFAULT_COUNT = 40
MIN_COUNT = 6
MAX_COUNT = 120
#: A photograph on the screen, before the music's beat moves the cut.
PHOTO_SECONDS = 4.0
#: The shortest a photograph is shown when a cut is moved onto a beat.
MIN_PHOTO_SECONDS = 2.6
#: How far a cut may move to land on a beat.
BEAT_REACH = 0.6
#: The longest stretch taken from one video clip.
CLIP_SECONDS = 5.0
#: Crossfade between two shots.
FADE = 0.8
TITLE_SECONDS = 4.5
END_SECONDS = 4.0
#: The longest a voice story is told for in a film; a longer one is cut at
#: the end of the last thing said before this.
MAX_STORY_SECONDS = 45.0
#: Quiet around a story, so it neither starts on the cut nor ends on it.
STORY_LEAD = 0.8
STORY_TAIL = 1.2
#: The music's level on its own, and under a voice or a clip's own sound.
MUSIC_LEVEL = 0.8
MUSIC_UNDER = 0.18
DUCK_RAMP = 0.5
#: How much of the frame a photograph's slow move closes in by.
ZOOM = 0.84
#: A photograph much narrower than the frame (a portrait or a square in a
#: landscape film) is shown whole over a blurred copy of itself instead of
#: cropped; a 4:3 photograph is cropped to the frame.
BACKDROP_BELOW = 0.62
MAX_FILM_SECONDS = 20 * 60
SUBTITLES = ("none", "spoken", "english", "both")
#: How a film moves, as an iPhone memory's mood sets its pace: how long a
#: photograph stays and how long the crossfade into the next one lasts.
PACES = {"gentle": (5.0, 1.2), "steady": (PHOTO_SECONDS, FADE), "lively": (2.6, 0.35)}
#: The colour of the whole film (Photos' "looks"); titles stay white.
LOOKS = ("natural", "warm", "faded", "mono")
#: A Live Photo's moment of movement, when one is shown moving.
LIVE_SECONDS = 3.0

#: The kinds of film: the moments of an album, a trip or a person; one
#: person growing up, face held still while the years go by; and then and
#: now, the same person years apart, side by side.
STYLES = ("film", "growing_up", "then_and_now")
#: Seconds each photograph of a growing-up film stays, and its fade, by pace:
#: quick, so the face seems to grow rather than the photographs to change.
GROWING_PACES = {"gentle": (2.6, 0.9), "steady": (1.8, 0.7), "lively": (1.2, 0.5)}
#: Seconds each pair of a then-and-now film stays, by pace.
PAIR_PACES = {"gentle": (6.5, 1.2), "steady": (5.0, 0.9), "lively": (3.8, 0.6)}
#: Seconds a chapter's heading stays.
CHAPTER_SECONDS = 3.2
#: Where the eyes are put in a growing-up film: the middle between them,
#: as a share of the frame, and how far apart they are, as a share of its
#: width. A child's face and a grown man's then sit in the same place.
EYES_AT = (0.5, 0.42)
EYES_APART = 0.11
#: The same for each half of a then-and-now pair.
PAIR_EYES_APART = 0.2
#: A photograph more tilted than this is not turned level, only placed.
MOST_TILT = math.radians(25)
#: How far an aligned photograph closes in over its shot.
ALIGNED_ZOOM = 0.04
#: Seconds the second photograph of a pair takes to open beside the first.
PAIR_REVEAL = 0.9


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ease(x: float) -> float:
    """0 → 1 with a gentle start and finish (half a cosine)."""
    x = _clamp(x, 0.0, 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * x)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _aspect(item: dict[str, Any]) -> float:
    w, h = item.get("width") or 0, item.get("height") or 0
    return (w / h) if w and h else 1.5


def frame_boxes(item: dict[str, Any], frame_aspect: float, index: int
                ) -> tuple[list[float], list[float]]:
    """Where a photograph's slow move starts and ends, as ``[x, y, w, h]`` of
    the upright photograph from 0 to 1.

    The window has the frame's shape. Without faces it closes in on (or opens
    out from) the middle, drifting a little to one side; with faces it closes
    in on them, so the move ends on whoever the photograph is of. Even shots
    close in and odd ones open out, which keeps a film from feeling like the
    same move forty times.
    """
    a = _aspect(item)
    if a >= frame_aspect:
        full = [frame_aspect / a, 1.0]
    else:
        full = [1.0, a / frame_aspect]
    fw, fh = full
    zw, zh = fw * ZOOM, fh * ZOOM
    faces = [f for f in item.get("faces") or [] if len(f) == 4 and f[2] > 0 and f[3] > 0]
    if faces:
        left = min(f[0] for f in faces)
        top = min(f[1] for f in faces)
        right = max(f[0] + f[2] for f in faces)
        bottom = max(f[1] + f[3] for f in faces)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        # Faces sit a little above the middle of a well-framed shot.
        cy += min(zh * 0.08, (bottom - top) * 0.25)
        if right - left > zw * 0.9 or bottom - top > zh * 0.9:
            zw, zh = fw, fh                         # a crowd: move across, not in
    else:
        drift = 0.04 if index % 4 < 2 else -0.04
        cx, cy = 0.5 + drift, 0.5
    fx, fy = 0.5 - fw / 2, 0.5 - fh / 2
    zx = _clamp(cx - zw / 2, 0.0, 1.0 - zw)
    zy = _clamp(cy - zh / 2, 0.0, 1.0 - zh)
    if zw == fw and zh == fh:
        # Same size: a slow pan from one side of the faces' window to the other.
        span = 1.0 - fw
        start = [_clamp(zx - span * 0.15, 0, span), fy, fw, fh]
        end = [_clamp(zx + span * 0.15, 0, span), fy, fw, fh]
        return (start, end) if index % 2 == 0 else (end, start)
    wide, close = [fx, fy, fw, fh], [zx, zy, zw, zh]
    return (wide, close) if index % 2 == 0 else (close, wide)


def _story_cut(story: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    """How long of a story is told, and its pieces within that."""
    length = float(story.get("duration") or 0.0)
    pieces = [s for s in story.get("segments") or []
              if isinstance(s, dict) and float(s.get("end") or 0) > float(s.get("start") or 0)]
    if length <= 0 and pieces:
        length = max(float(s["end"]) for s in pieces)
    if length <= MAX_STORY_SECONDS:
        return length, pieces
    ends = [float(s["end"]) for s in pieces if float(s["end"]) <= MAX_STORY_SECONDS]
    cut = max(ends) if ends and max(ends) >= MAX_STORY_SECONDS / 2 else MAX_STORY_SECONDS
    return cut, [s for s in pieces if float(s["end"]) <= cut + 0.01]


def _sub_lines(piece: dict[str, Any], language: str, mode: str) -> list[str]:
    said = " ".join(str(piece.get("text") or "").split())
    english = " ".join(str(piece.get("english") or "").split())
    if language == "en" or english == said:
        english = ""
    if mode == "spoken":
        return [said] if said else []
    if mode == "english":
        return [english or said] if (english or said) else []
    if mode == "both":
        return [line for line in (said, english) if line]
    return []


def _snap(cut: float, beats: Sequence[float], earliest: float) -> float:
    """*cut* moved onto the nearest beat within reach, never before *earliest*."""
    if not beats:
        return cut
    best = min(beats, key=lambda b: abs(b - cut))
    if abs(best - cut) <= BEAT_REACH and best >= earliest:
        return best
    return cut


def plan(spec: dict[str, Any], *, beats: Sequence[float] | None = None) -> dict[str, Any]:
    """The timeline of the film *spec* describes.

    *spec* has ``items`` (in the order they are shown; each with ``kind``,
    ``path``, ``width``, ``height``, ``rotation``, ``when``, ``faces``, and a
    ``duration`` for a clip, ``has_audio`` when it has sound, and ``story``
    when somebody told one about it), a ``title`` and ``subtitle``, ``music``
    (a ``path`` and ``duration``, or None), ``subtitles`` (one of
    :data:`SUBTITLES`) and ``size``. *beats* are the music's beats, in seconds.
    """
    width, height = SIZES.get(spec.get("size") or DEFAULT_SIZE, SIZES[DEFAULT_SIZE])
    frame_aspect = width / height
    style = spec.get("style") if spec.get("style") in STYLES else "film"
    pace = spec.get("pace") or "steady"
    photo_seconds, fade = PACES.get(pace, PACES["steady"])
    if style == "growing_up":
        photo_seconds, fade = GROWING_PACES.get(pace, GROWING_PACES["steady"])
    pair_seconds, pair_fade = PAIR_PACES.get(pace, PAIR_PACES["steady"])
    if style == "then_and_now":
        fade = pair_fade
    shortest = min(MIN_PHOTO_SECONDS, photo_seconds * 0.8)
    mode = spec.get("subtitles") if spec.get("subtitles") in SUBTITLES else "both"
    beats = sorted(float(b) for b in (beats or []))
    items = list(spec.get("items") or [])
    shots: list[dict[str, Any]] = []
    narration: list[dict[str, Any]] = []
    clip_sound: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []

    t = 0.0
    # The title is set over the first photograph and the closing words over
    # the last (a clip cannot be held still behind words).
    pictures = [n for n, item in enumerate(items) if (item.get("kind") or "picture") == "picture"]
    if items:
        shots.append({"kind": "title", "start": 0.0, "dur": TITLE_SECONDS,
                      "item": pictures[0] if pictures else None,
                      "lines": [spec.get("title") or "", spec.get("subtitle") or ""]})
        t = TITLE_SECONDS - fade
    last_month = None
    months = spec.get("months") or []
    chapters = {int(c["at"]): c for c in spec.get("chapters") or []
                if isinstance(c, dict) and isinstance(c.get("at"), int)
                and 0 <= c["at"] < len(items) and str(c.get("title") or "").strip()}
    for index, item in enumerate(items):
        if index in chapters:
            # A chapter's heading, over the first photograph of the chapter.
            behind = next((n for n in pictures if n >= index), pictures[-1] if pictures else None)
            chapter = chapters[index]
            shots.append({"kind": "chapter", "start": round(t, 3), "dur": CHAPTER_SECONDS,
                          "item": behind if style == "film" else None,
                          "lines": [str(chapter.get("title") or ""), str(chapter.get("line") or "")]})
            t += CHAPTER_SECONDS - fade
        shot: dict[str, Any] = {"kind": item.get("kind") or "picture", "start": round(t, 3),
                                "item": index}
        # A growing-up film moves too quickly for a story; a pair has two photographs.
        story = item.get("story") if spec.get("stories", True) and style == "film" else None
        if shot["kind"] == "pair":
            shot["dur"] = pair_seconds
            for side, x in (("then", 0.25), ("now", 0.75)):
                text = str((item.get(side) or {}).get("label") or "")
                if text:
                    labels.append({"start": round(t + fade, 3),
                                   "end": round(t + pair_seconds - fade / 2, 3),
                                   "text": text, "at": [x, 0.9, "center"], "keep": True})
        elif shot["kind"] == "live":
            # A Live Photo's few seconds of movement, from their beginning:
            # the moment the shutter was pressed is near their middle.
            length = float(item.get("duration") or 0.0)
            shot.update(dur=_clamp(length - 0.1, 1.2, LIVE_SECONDS) if length else LIVE_SECONDS,
                        offset=0.0)
        elif shot["kind"] == "video":
            length = float(item.get("duration") or 0.0)
            dur = _clamp(length - 0.2, 1.5, CLIP_SECONDS) if length > 0 else CLIP_SECONDS
            offset = max(0.0, (length - dur) / 2) if length > dur + 2 else min(0.5, max(0.0, length - dur))
            shot.update(dur=dur, offset=round(offset, 3))
            if item.get("has_audio"):
                clip_sound.append({"item": index, "at": t, "offset": offset, "dur": dur})
        else:
            dur = photo_seconds
            if story:
                told, pieces = _story_cut(story)
                if told > 0:
                    at = t + STORY_LEAD
                    narration.append({"item": index, "at": round(at, 3), "dur": round(told, 3)})
                    dur = max(dur, STORY_LEAD + told + STORY_TAIL)
                    for piece in pieces:
                        lines = _sub_lines(piece, str(story.get("language") or ""), mode)
                        if lines:
                            subtitles.append({"start": round(at + float(piece["start"]), 3),
                                              "end": round(at + min(float(piece["end"]), told), 3),
                                              "lines": lines})
            if style == "growing_up" and item.get("eyes"):
                shot["align"] = True
                if item.get("label"):
                    labels.append({"start": round(t + fade / 2, 3),
                                   "end": round(t + dur - fade / 2, 3),
                                   "text": str(item["label"]), "at": [0.5, 0.9, "center"],
                                   "keep": True})
            else:
                shot["from"], shot["to"] = frame_boxes(item, frame_aspect, index)
                shot["backdrop"] = _aspect(item) < frame_aspect * BACKDROP_BELOW
            shot["dur"] = dur
        # The cut — the middle of the fade into the next shot — onto a beat.
        cut = _snap(t + shot["dur"] - fade / 2, beats, t + shortest)
        if shot["kind"] == "picture" and not story:
            shot["dur"] = round(cut + fade / 2 - t, 3)
        shot["dur"] = round(shot["dur"], 3)
        when = item.get("when") or 0
        if when and len(months) == 12 and style == "film" and not chapters:
            stamp = time.gmtime(when)
            month = (stamp.tm_year, stamp.tm_mon)
            if month != last_month:
                labels.append({"start": round(t + fade, 3), "end": round(t + shot["dur"] - fade, 3),
                               "text": f"{months[stamp.tm_mon - 1]} {stamp.tm_year}"})
                last_month = month
        shots.append(shot)
        t += shot["dur"] - fade
        if t > MAX_FILM_SECONDS:
            break
    if items:
        shots.append({"kind": "end", "start": round(t, 3), "dur": END_SECONDS,
                      "item": pictures[-1] if pictures else None,
                      "lines": [spec.get("closing") or ""]})
        t += END_SECONDS
    # A month that only changes once is not worth saying (a growing-up
    # film's years and a pair's are kept whatever their number).
    if len([lb for lb in labels if not lb.get("keep")]) < 2:
        labels = [lb for lb in labels if lb.get("keep")]
    duration = round(t, 3)
    loud = sorted([(n["at"], n["at"] + n["dur"]) for n in narration]
                  + [(c["at"], c["at"] + c["dur"]) for c in clip_sound])
    music = spec.get("music") if isinstance(spec.get("music"), dict) else None
    look = spec.get("look") if spec.get("look") in LOOKS else "natural"
    return {"size": [width, height], "fps": FPS, "duration": duration, "fade": fade,
            "look": look, "style": style, "shots": shots,
            "narration": narration, "clip_sound": clip_sound, "subtitles": subtitles,
            "labels": labels, "duck": [[round(a, 3), round(b, 3)] for a, b in loud],
            "music": {"path": music["path"]} if music and music.get("path") else None}


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

#: About one moment in seven is a video clip, where the source has them.
CLIP_SHARE = 7
#: A clip shorter than this is a mistake, and one longer than this is a
#: recording of a whole event, whose middle five seconds say little.
CLIP_LENGTH = (2.0, 600.0)


def pick(rows: Sequence[Any], count: int = DEFAULT_COUNT, *,
         screens: Iterable[int] = (), faces: dict[int, int] | None = None,
         signals: dict[int, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The moments for a film, oldest first: the books' choice of photographs
    (:func:`ninaivu.media.books.pick`) and, where the source has videos, about
    one clip in :data:`CLIP_SHARE`, the best of them spread across its time."""
    from . import books                                        # noqa: PLC0415

    faces, signals = faces or {}, signals or {}
    clips = []
    for row in rows:
        if books._field(row, "kind") != "video":                # noqa: SLF001
            continue
        length = float(books._field(row, "duration", 0) or 0)  # noqa: SLF001
        if not CLIP_LENGTH[0] <= length <= CLIP_LENGTH[1]:
            continue
        item = dict(row)
        item["score"] = books.score(row, faces.get(int(item["id"]), 0),
                                    signals.get(int(item["id"])))
        clips.append(item)
    wanted_clips = min(len(clips), max(0, round(count / CLIP_SHARE)))
    photos = [r for r in rows if books._field(r, "kind", "picture") == "picture"]  # noqa: SLF001
    chosen = books.pick(photos, max(1, count - wanted_clips), screens=screens, faces=faces,
                        signals=signals)
    if wanted_clips < count - len(chosen):
        wanted_clips = min(len(clips), count - len(chosen))
    if clips and wanted_clips:
        clips.sort(key=lambda r: (books.when(r), int(r["id"])))
        chosen += books._spread(clips, wanted_clips) if len(clips) > wanted_clips else clips  # noqa: SLF001
    chosen.sort(key=lambda r: (books.when(r), int(r["id"])))
    return chosen


#: The smallest face a growing-up or then-and-now film holds by the eyes, as
#: a share of the photograph's width: smaller, and holding it fills the
#: screen with a blur of a few dozen pixels.
SMALLEST_FACE = 0.06
#: Pairs in a then-and-now film, and the fewest years between the two of a pair.
PAIRS = 6
PAIR_YEARS = 5


def face_score(face: dict[str, Any]) -> float:
    """How well a photograph shows the person's face: big, sharp, facing us."""
    size = float(face.get("size") or 0.0)
    quality = float(face.get("quality") or 0.0)
    return size * (0.5 + min(1.0, max(0.0, quality)))


def _held(rows: Sequence[Any], faces: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    from . import books                                        # noqa: PLC0415
    out = []
    for row in rows:
        face = faces.get(int(books._field(row, "id")))         # noqa: SLF001
        if not face or not face.get("eyes") or float(face.get("size") or 0) < SMALLEST_FACE:
            continue
        if books._field(row, "kind", "picture") != "picture":  # noqa: SLF001
            continue
        when = books.when(row)
        if not when:
            continue
        item = dict(row)
        item["when"], item["face_score"] = when, face_score(face)
        out.append(item)
    return out


def pick_growing_up(rows: Sequence[Any], faces: dict[int, dict[str, Any]],
                    count: int = DEFAULT_COUNT) -> list[dict[str, Any]]:
    """One person over the years, oldest first: the photographs that show
    their face best, shared out across the years so that no year with many
    photographs crowds out the years with a few.

    *faces* is the person's face in each photograph: ``size`` (its width as a
    share of the photograph's), ``quality`` and ``eyes``. A photograph without
    their face, or with it too small to hold, or with no date, is left out.
    """
    held = _held(rows, faces)
    years: dict[int, list[dict[str, Any]]] = {}
    for item in held:
        years.setdefault(time.gmtime(item["when"]).tm_year, []).append(item)
    for found in years.values():
        found.sort(key=lambda r: -r["face_score"])
    chosen: list[dict[str, Any]] = []
    # Round by round, the best not yet chosen of every year.
    while len(chosen) < count and any(years.values()):
        for year in sorted(years):
            if years[year] and len(chosen) < count:
                chosen.append(years[year].pop(0))
    chosen.sort(key=lambda r: (r["when"], int(r["id"])))
    return chosen


def pick_then_and_now(rows: Sequence[Any], faces: dict[int, dict[str, Any]],
                      pairs: int = PAIRS) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pairs of the same person years apart, each an early photograph and a
    late one, at least :data:`PAIR_YEARS` apart, oldest pair first. The
    earliest third of the person's years is matched with the latest third,
    best face with best face, so every pair is a clear then and a clear now."""
    held = sorted(_held(rows, faces), key=lambda r: (r["when"], int(r["id"])))
    if len(held) < 2:
        return []
    first, last = held[0]["when"], held[-1]["when"]
    span = last - first
    if span < PAIR_YEARS * 365.25 * 86400:
        return []
    early = sorted((r for r in held if r["when"] <= first + span / 3), key=lambda r: -r["face_score"])
    late = sorted((r for r in held if r["when"] >= last - span / 3), key=lambda r: -r["face_score"])
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used: set[int] = set()
    for then in early:
        if len(out) >= pairs:
            break
        match = next((now for now in late if int(now["id"]) not in used
                      and now["when"] - then["when"] >= PAIR_YEARS * 365.25 * 86400), None)
        if match is None:
            continue
        used.update((int(then["id"]), int(match["id"])))
        out.append((then, match))
    out.sort(key=lambda pair: (pair[0]["when"], int(pair[0]["id"])))
    return out


# ---------------------------------------------------------------------------
# The music's beat
# ---------------------------------------------------------------------------

BEAT_RATE = 11025
_HOP = 256
_ONSET_LAG = 0.062


def beats_of(samples: Any, rate: int = BEAT_RATE, *, low_bpm: float = 60.0,
             high_bpm: float = 160.0) -> list[float]:
    """The beats of mono *samples* (a numpy array), in seconds.

    The strength of new sound in each short window (how much the spectrum
    grew since the last one), the tempo that strength repeats at most, then
    the phase of that tempo that lands on the most of it. Enough to put a cut
    on the beat of a song or a veena piece; not a musicologist.
    """
    import numpy as np                                          # noqa: PLC0415

    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    size = 1024
    if x.size < size * 8:
        return []
    count = 1 + (x.size - size) // _HOP
    window = np.hanning(size).astype(np.float32)
    idx = np.arange(size)[None, :] + _HOP * np.arange(count)[:, None]
    spectrum = np.log1p(np.abs(np.fft.rfft(x[idx] * window, axis=1)))
    flux = np.maximum(0.0, np.diff(spectrum, axis=0)).sum(axis=1)
    flux = flux - np.convolve(flux, np.ones(16) / 16, mode="same")
    flux = np.maximum(flux, 0.0)
    if not flux.any():
        return []
    fps = rate / _HOP
    lags = np.arange(int(fps * 60 / high_bpm), int(fps * 60 / low_bpm) + 1)
    centred = flux - flux.mean()
    corr = np.array([float(np.dot(centred[:-lag], centred[lag:])) for lag in lags])
    # Prefer tempi near 100 bpm a little, so a piece is not counted at half time.
    bpm = 60 * fps / lags
    corr = corr * np.exp(-0.5 * (np.log2(bpm / 100.0) / 0.9) ** 2)
    best = int(np.argmax(corr))
    period = float(lags[best])
    if 0 < best < len(corr) - 1:
        # Between two lags: the top of the parabola through the peak and its
        # neighbours, so a tempo is not rounded to whole windows (at 23 ms a
        # window, rounding drifts a cut by a beat within a minute).
        a, b, c = corr[best - 1], corr[best], corr[best + 1]
        if a - 2 * b + c < 0:
            period += 0.5 * (a - c) / (a - 2 * b + c)
    whole = int(round(period))
    head = flux[: int(fps * 8)] if flux.size > fps * 8 else flux
    phase = int(np.argmax([float(head[p::whole].sum()) for p in range(whole)]))
    # Followed beat by beat: each next beat is the strongest new sound near
    # where the tempo says it should be, so a player who drifts is followed.
    reach = max(1, int(period * 0.12))
    floor = float(flux.mean())
    found: list[float] = []
    pos = float(phase)
    while pos < flux.size:
        lo, hi = max(0, int(pos) - reach), min(flux.size, int(pos) + reach + 1)
        window = flux[lo:hi]
        if window.size and float(window.max()) > floor:
            pos = float(lo + int(np.argmax(window)))
        found.append(pos)
        pos += period
    # Flux at index i is the change into window i + 1, whose start is a
    # little before the sound is heard in it (measured on click tracks: by
    # 62 ms at this rate and window).
    return [round((p + 1) * _HOP / rate + _ONSET_LAG, 3) for p in found]


def music_beats(path: str | Path, ffmpeg: str, seconds: float) -> list[float]:
    """The beats of the music file's first *seconds*, or none if it cannot be read."""
    import numpy as np                                          # noqa: PLC0415

    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-protocol_whitelist", "file", "-i", str(path),
             "-t", f"{max(1.0, seconds):.2f}", "-ac", "1", "-ar", str(BEAT_RATE),
             "-f", "f32le", "pipe:1"], capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    return beats_of(np.frombuffer(proc.stdout, dtype=np.float32))


# ---------------------------------------------------------------------------
# Sound
# ---------------------------------------------------------------------------

def duck_expression(intervals: Sequence[Sequence[float]]) -> str:
    """ffmpeg's ``volume`` expression for the music: :data:`MUSIC_LEVEL`,
    sliding down to :data:`MUSIC_UNDER` over :data:`DUCK_RAMP` seconds around
    every stretch somebody speaks or a clip has its own sound."""
    if not intervals:
        return f"{MUSIC_LEVEL}"
    merged: list[list[float]] = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1] + DUCK_RAMP * 2:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    ramps = [f"clip(min((t-{a - DUCK_RAMP:.3f})/{DUCK_RAMP},({b + DUCK_RAMP:.3f}-t)/{DUCK_RAMP}),0,1)"
             for a, b in merged]
    under = ramps[0]
    for ramp in ramps[1:]:
        under = f"max({under},{ramp})"
    return f"{MUSIC_LEVEL}-{MUSIC_LEVEL - MUSIC_UNDER:.3f}*{under}"


def mix_command(ffmpeg: str, timeline: dict[str, Any], items: Sequence[dict[str, Any]],
                out: Path) -> list[str]:
    """The ffmpeg command that writes the film's whole sound to *out*."""
    total = float(timeline["duration"])
    args = [ffmpeg, "-v", "error", "-y"]
    chains: list[str] = []
    labels: list[str] = []
    n = 0
    music = timeline.get("music")
    if music:
        args += ["-protocol_whitelist", "file", "-stream_loop", "-1", "-i", music["path"]]
        fade_out = min(4.0, total / 4)
        chains.append(f"[{n}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                      f"aformat=sample_rates=48000:channel_layouts=stereo,"
                      f"volume='{duck_expression(timeline.get('duck') or [])}':eval=frame,"
                      f"afade=t=in:d=1.5,afade=t=out:st={max(0.0, total - fade_out):.3f}:d={fade_out:.3f}[m]")
        labels.append("[m]")
        n += 1
    for kind, gain in (("narration", 1.0), ("clip_sound", 0.9)):
        for event in timeline.get(kind) or []:
            item = items[event["item"]]
            path = (item.get("story") or {}).get("path") if kind == "narration" else item.get("path")
            if not path:
                continue
            offset = float(event.get("offset") or 0.0)
            dur = float(event["dur"])
            delay = int(round(float(event["at"]) * 1000))
            args += ["-protocol_whitelist", "file", "-i", str(path)]
            fade = min(0.3, dur / 4)
            chains.append(f"[{n}:a]atrim={offset:.3f}:{offset + dur:.3f},asetpts=PTS-STARTPTS,"
                          f"aformat=sample_rates=48000:channel_layouts=stereo,"
                          f"afade=t=in:d=0.15,afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f},"
                          f"volume={gain},adelay={delay}|{delay}[s{n}]")
            labels.append(f"[s{n}]")
            n += 1
    args += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    labels.append(f"[{n}:a]")
    chains.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
                  f"atrim=0:{total:.3f},alimiter=limit=0.95[out]")
    return [*args, "-filter_complex", ";".join(chains), "-map", "[out]",
            "-c:a", "aac", "-b:a", "192k", str(out)]


def srt(subtitles: Sequence[dict[str, Any]], line: int) -> str:
    """One language's subtitles as SubRip: *line* 0 is what was said, 1 the English."""
    def stamp(s: float) -> str:
        ms = int(round(s * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    blocks = []
    for sub in subtitles:
        lines = sub["lines"]
        text = lines[line] if line < len(lines) else ""
        if text:
            blocks.append(f"{len(blocks) + 1}\n{stamp(sub['start'])} --> {stamp(sub['end'])}\n{text}\n")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

def _open_upright(item: dict[str, Any], edge: int) -> Image.Image:
    """The photograph upright, as the gallery shows it, at least *edge* on its long side."""
    from . import media, upright                              # noqa: PLC0415

    image, _ = media.open_for_index(item["path"], edge)
    try:
        turned = upright.apply(image, int(item.get("rotation") or 0))
        if turned.mode != "RGB":
            turned = turned.convert("RGB")
        if max(turned.size) > edge * 1.5:
            scale = edge / max(turned.size)
            turned = turned.resize((max(1, round(turned.width * scale)),
                                    max(1, round(turned.height * scale))),
                                   Image.Resampling.LANCZOS, reducing_gap=3.0)
        return turned if turned is not image else image.copy()
    finally:
        image.close()


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    w, h = size
    scale = max(w / image.width, h / image.height)
    resized = image.resize((max(w, round(image.width * scale)), max(h, round(image.height * scale))),
                           Image.Resampling.BILINEAR)
    x, y = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((x, y, x + w, y + h))


def backdrop(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """*image* whole, centred over a dark, blurred copy of itself filling *size*."""
    w, h = size
    small = _cover(image, (max(1, w // 8), max(1, h // 8)))
    blurred = small.filter(ImageFilter.GaussianBlur(radius=max(2, w // 160)))
    canvas = blurred.resize(size, Image.Resampling.BILINEAR)
    canvas = Image.blend(canvas, Image.new("RGB", size, (0, 0, 0)), 0.45)
    scale = min(w / image.width, h / image.height)
    fitted = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                          Image.Resampling.LANCZOS)
    canvas.paste(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
    return canvas


def eyes_in(item: dict[str, Any], image: Image.Image) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The two eyes of the person a shot is about, in *image*'s pixels, left first."""
    eyes = item.get("eyes")
    try:
        (ax, ay), (bx, by) = ((float(x), float(y)) for x, y in eyes)
    except (TypeError, ValueError):
        return None
    left, right = sorted(((ax * image.width, ay * image.height), (bx * image.width, by * image.height)))
    if math.dist(left, right) < 2:
        return None
    return left, right


def align_matrix(eyes: tuple[tuple[float, float], tuple[float, float]], mid: tuple[float, float],
                 apart: float) -> tuple[float, float, float, float, float, float]:
    """The affine map, for :meth:`PIL.Image.Image.transform`, that puts *eyes*
    (in the photograph's pixels) level, *apart* pixels from each other, with
    their middle at *mid* (in the frame's pixels).

    PIL wants the inverse: for each pixel of the frame, where to read the
    photograph. A head tilted more than :data:`MOST_TILT` is placed, not
    turned (somebody lying on a sofa should not tip the room over).
    """
    (lx, ly), (rx, ry) = eyes
    angle = math.atan2(ry - ly, rx - lx)
    if abs(angle) > MOST_TILT:
        angle = 0.0
    k = apart / max(1e-6, math.dist((lx, ly), (rx, ry)))
    cos, sin = math.cos(angle), math.sin(angle)
    sx, sy = (lx + rx) / 2, (ly + ry) / 2
    tx, ty = mid
    a, b, d, e = cos / k, -sin / k, sin / k, cos / k
    return (a, b, sx - (a * tx + b * ty), d, e, sy - (d * tx + e * ty))


def _aligned_source(item: dict[str, Any], apart: float) -> Image.Image:
    """The photograph decoded large enough that its eyes are *apart* pixels
    from each other without being blown up much (a small face in a big
    photograph is decoded large; at most 4096 pixels on its long side)."""
    w, h = item.get("width") or 0, item.get("height") or 0
    edge = 1600
    eyes = item.get("eyes")
    try:
        (ax, ay), (bx, by) = eyes
        if w and h:
            gap = math.hypot((float(bx) - float(ax)) * w, (float(by) - float(ay)) * h) / max(w, h)
            if gap > 0:
                edge = int(apart / gap * 1.1)
    except (TypeError, ValueError):
        pass
    return _open_upright(item, max(800, min(4096, edge)))


class Aligned:
    """A photograph held by the eyes: the same face in the same place, shot
    after shot, while the years go by (a growing-up film, each half of a
    then-and-now pair). Over a blurred copy of itself, where it does not reach."""

    def __init__(self, item: dict[str, Any], size: tuple[int, int], *, apart: float,
                 mid: tuple[float, float], zoom: float = ALIGNED_ZOOM) -> None:
        self.size = size
        self.mid = mid
        self.apart = apart
        self.zoom = zoom
        source = _aligned_source(item, apart)
        self.eyes = eyes_in(item, source)
        self.ground = backdrop(source, size) if self.eyes else None
        if self.eyes:
            self.image = source.convert("RGBA")
            source.close()
        else:
            self.image = source

    def frame(self, p: float) -> Image.Image:
        """The frame *p* of the way (0 to 1) through the shot."""
        if not self.eyes:
            return _cover(self.image, self.size)
        apart = self.apart * (1 + self.zoom * ease(p))
        matrix = align_matrix(self.eyes, self.mid, apart)
        layer = self.image.transform(self.size, Image.Transform.AFFINE, matrix,
                                     Image.Resampling.BILINEAR, fillcolor=(0, 0, 0, 0))
        out = self.ground.copy()
        out.paste(layer, (0, 0), layer)
        return out

    def close(self) -> None:
        self.image.close()
        if self.ground is not None:
            self.ground.close()


class Pair:
    """Then and now: the same person years apart, side by side, eyes level
    across the two. The second opens beside the first."""

    def __init__(self, item: dict[str, Any], shot: dict[str, Any], size: tuple[int, int]) -> None:
        w, h = size
        self.size = size
        self.shot = shot
        half = (w // 2, h)
        self.sides = [Aligned(item[side], half, apart=half[0] * PAIR_EYES_APART,
                              mid=(half[0] * 0.5, h * EYES_AT[1]))
                      for side in ("then", "now")]

    def frame(self, local: float) -> Image.Image:
        w, h = self.size
        p = local / max(0.001, self.shot["dur"])
        out = Image.new("RGB", self.size, (12, 12, 14))
        out.paste(self.sides[0].frame(p), (0, 0))
        reveal = ease(local / PAIR_REVEAL)
        if reveal > 0:
            now = self.sides[1].frame(p)
            shown = max(1, round(now.width * reveal))
            out.paste(now.crop((0, 0, shown, h)), (w // 2, 0))
        line = max(2, w // 480)
        ImageDraw.Draw(out).rectangle((w // 2 - line // 2, 0, w // 2 + (line + 1) // 2 - 1, h),
                                      fill=(245, 240, 232))
        return out

    def close(self) -> None:
        for side in self.sides:
            side.close()


class Still:
    """One photograph's shot: its pixels decoded once, a frame at any moment."""

    #: Set for a photograph held by the eyes (a growing-up film).
    aligned: Aligned | None = None

    def __init__(self, item: dict[str, Any] | None, shot: dict[str, Any],
                 size: tuple[int, int]) -> None:
        self.size = size
        self.shot = shot
        w, h = size
        if shot.get("align") and item is not None:
            self.aligned = Aligned(item, size, apart=w * EYES_APART,
                                   mid=(w * EYES_AT[0], h * EYES_AT[1]))
            self.image = Image.new("RGB", (1, 1))
            return
        if item is None:
            # A title with no photograph behind it: a deep plain ground.
            self.image = Image.new("RGB", size, (24, 22, 30))
            self.boxes = ([0.0, 0.0, 1.0, 1.0], [0.0, 0.0, 1.0, 1.0])
        elif shot.get("backdrop") or shot["kind"] in ("title", "end", "chapter"):
            # Composed a little larger than the frame, so it can close in.
            canvas_size = (round(w / ZOOM ** 0.5), round(h / ZOOM ** 0.5))
            source = _open_upright(item, max(canvas_size))
            try:
                self.image = backdrop(source, canvas_size)
            finally:
                source.close()
            cw, ch = canvas_size
            inner = [(cw - w) / 2 / cw, (ch - h) / 2 / ch, w / cw, h / ch]
            whole = [0.0, 0.0, 1.0, 1.0]
            self.boxes = (whole, inner) if (shot["item"] or 0) % 2 == 0 else (inner, whole)
        else:
            fw = max(shot["from"][2], shot["to"][2])
            # Decoded so that the widest window still has the frame's pixels.
            need = int(w / fw * 1.05) if fw else w
            source = _open_upright(item, max(need, w))
            self.image = source
            self.boxes = (shot["from"], shot["to"])
        if shot["kind"] in ("title", "end", "chapter") and item is not None:
            dark = 0.5 if shot["kind"] == "chapter" else 0.35
            self.image = Image.blend(self.image, Image.new("RGB", self.image.size, (0, 0, 0)), dark)

    def frame(self, local: float) -> Image.Image:
        if self.aligned is not None:
            return self.aligned.frame(local / max(0.001, self.shot["dur"]))
        p = ease(local / max(0.001, self.shot["dur"]))
        a, b = self.boxes
        x, y, bw, bh = (a[i] + (b[i] - a[i]) * p for i in range(4))
        iw, ih = self.image.size
        extent = (x * iw, y * ih, (x + bw) * iw, (y + bh) * ih)
        return self.image.transform(self.size, Image.Transform.EXTENT, extent,
                                    Image.Resampling.BILINEAR)

    def close(self) -> None:
        self.image.close()
        if self.aligned is not None:
            self.aligned.close()


def clip_filter(size: tuple[int, int]) -> str:
    """A clip at the film's size and rate: whole, over a blurred copy of itself."""
    w, h = size
    return (f"split[a][b];[a]scale={w // 8}:{h // 8}:force_original_aspect_ratio=increase,"
            f"crop={w // 8}:{h // 8},boxblur=6:2,scale={w}:{h},eq=brightness=-0.12[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},format=rgb24")


class Clip:
    """One video clip's shot, its frames read from ffmpeg as they are needed.

    ffmpeg is started at the first frame asked for, so a part of the film
    that begins in the middle of a clip (:func:`render` makes the film in
    parts, side by side) starts the clip there and not at its beginning.
    """

    def __init__(self, ffmpeg: str, item: dict[str, Any], shot: dict[str, Any],
                 size: tuple[int, int]) -> None:
        self.ffmpeg, self.path = ffmpeg, item["path"]
        self.size = size
        self.shot = shot
        self.bytes = size[0] * size[1] * 3
        self.last: Image.Image | None = None
        self.read = 0
        self.proc: subprocess.Popen | None = None

    def _open(self, local: float) -> None:
        self.read = int(local * FPS)
        start = float(self.shot.get("offset") or 0) + self.read / FPS
        self.proc = subprocess.Popen(
            [self.ffmpeg, "-v", "error", "-protocol_whitelist", "file",
             "-ss", f"{start:.3f}", "-t", f"{self.shot['dur'] - self.read / FPS + 0.5:.3f}",
             "-i", self.path, "-an", "-filter_complex", clip_filter(self.size),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def frame(self, local: float) -> Image.Image:
        if self.proc is None:
            self._open(local)
        wanted = int(local * FPS)
        while self.read <= wanted and self.proc is not None and self.proc.stdout is not None:
            data = self.proc.stdout.read(self.bytes)
            if len(data) < self.bytes:
                break                                 # the clip ran out: hold the last frame
            self.last = Image.frombytes("RGB", self.size, data)
            self.read += 1
        return self.last.copy() if self.last is not None else Image.new("RGB", self.size)

    def close(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.kill()
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        self.proc.wait()


# ---------------------------------------------------------------------------
# Words on the screen
# ---------------------------------------------------------------------------

class Lettering:
    """Titles, the month and subtitles, set in the books' fonts (Tamil with raqm)."""

    def __init__(self, size: tuple[int, int]) -> None:
        from .books import Type                                   # noqa: PLC0415

        self.size = size
        self.type = Type()
        self._cache: dict[Any, Image.Image] = {}

    def _layer(self) -> Image.Image:
        return Image.new("RGBA", self.size, (0, 0, 0, 0))

    def title(self, lines: Sequence[str], *, chapter: bool = False) -> Image.Image:
        key = ("title", tuple(lines), chapter)
        if key not in self._cache:
            w, h = self.size
            layer = self._layer()
            draw = ImageDraw.Draw(layer)
            main = self.type.printable(lines[0] if lines else "")
            sub = self.type.printable(lines[1] if len(lines) > 1 else "")
            # A chapter's heading is smaller than the film's title, and its
            # line can run to two rows.
            big = self.type.fit(main, max(22, h // (16 if chapter else 12)), int(w * 0.84), bold=True)
            small = self.type.fit(sub, max(16, h // (30 if chapter else 26)), int(w * 0.8))
            y = h * 0.5 + (big / 3 if not sub else 0)
            for dx, dy, fill in ((0, max(2, h // 360), (0, 0, 0, 150)), (0, 0, (255, 255, 255, 255))):
                if main:
                    self.type.draw(draw, w / 2 + dx, y + dy, main, big, fill, bold=True, align="center")
                if sub:
                    self.type.draw(draw, w / 2 + dx, y + big * 0.9 + dy, sub, small,
                                   (fill[0], fill[1], fill[2], int(fill[3] * 0.9)), align="center")
            self._cache[key] = layer
        return self._cache[key]

    def label(self, text: str, at: Sequence[Any] | None = None) -> Image.Image:
        """A few words: the month, top left, unless *at* says where (``[x, y,
        align]`` as shares of the frame: a growing-up film's year, a pair's)."""
        key = ("label", text, tuple(at or ()))
        if key not in self._cache:
            w, h = self.size
            layer = self._layer()
            draw = ImageDraw.Draw(layer)
            if at:
                size = max(16, h // 22)
                x, y, align = w * float(at[0]), h * float(at[1]), str(at[2] if len(at) > 2 else "left")
            else:
                size = max(14, h // 34)
                x, y, align = w * 0.045, h * 0.09, "left"
            text = self.type.printable(text)
            self.type.draw(draw, x + 1, y + 1, text, size, (0, 0, 0, 170), align=align)
            self.type.draw(draw, x, y, text, size, (255, 255, 255, 240), align=align)
            self._cache[key] = layer
        return self._cache[key]

    def subtitle(self, lines: Sequence[str]) -> Image.Image:
        key = ("sub", tuple(lines))
        if key not in self._cache:
            w, h = self.size
            layer = self._layer()
            draw = ImageDraw.Draw(layer)
            size = max(16, h // 24)
            rows: list[tuple[str, int]] = []
            for n, text in enumerate(lines[:2]):
                line_size = size if n == 0 else int(size * 0.82)
                for row in self.type.wrap(text, line_size, int(w * 0.8), 2):
                    rows.append((row, line_size))
            if rows:
                gap = int(size * 0.35)
                height = sum(s for _, s in rows) + gap * (len(rows) + 1)
                width = max(self.type.width(t, s) for t, s in rows) + size
                top = h * 0.93 - height
                draw.rounded_rectangle((w / 2 - width / 2, top, w / 2 + width / 2, top + height),
                                       radius=size // 2, fill=(0, 0, 0, 140))
                y = top + gap
                for text, line_size in rows:
                    y += line_size
                    self.type.draw(draw, w / 2, y - line_size * 0.2, text, line_size,
                                   (255, 255, 255, 255), align="center")
                    y += gap
            self._cache[key] = layer
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
        return self._cache[key]


def _curve(fn: Callable[[int], float]) -> list[int]:
    return [int(_clamp(round(fn(v)), 0, 255)) for v in range(256)]


#: Per-channel curves of each look: a lookup, so a 1080p frame costs a few ms.
_LOOK_CURVES = {
    "warm": (_curve(lambda v: v * 1.05 + 8), _curve(lambda v: v * 1.01 + 2),
             _curve(lambda v: v * 0.9)),
    "faded": tuple(_curve(lambda v, lift=lift: 22 + lift + v * 0.8) for lift in (6, 2, 0)),
}


def look(frame: Image.Image, name: str) -> Image.Image:
    """*frame* in one of :data:`LOOKS`."""
    if name == "mono":
        grey = frame.convert("L").point(_curve(lambda v: 10 + v * 0.93))
        return Image.merge("RGB", (grey, grey, grey))
    curves = _LOOK_CURVES.get(name)
    if not curves:
        return frame
    if name == "faded":
        frame = Image.blend(frame, frame.convert("L").convert("RGB"), 0.3)
    return frame.point([v for curve in curves for v in curve])


def _over(frame: Image.Image, layer: Image.Image, alpha: float) -> Image.Image:
    if alpha <= 0:
        return frame
    done = Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
    return done if alpha >= 1 else Image.blend(frame, done, alpha)


def _fade_in_out(t: float, start: float, end: float, ramp: float = 0.4) -> float:
    return _clamp(min((t - start) / ramp, (end - t) / ramp), 0.0, 1.0)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class Cancelled(Exception):
    """The person making the film asked for it to stop."""


def video_encoder(ffmpeg: str) -> list[str]:
    """The best H.264 encoder this ffmpeg has, with its settings."""
    try:
        listed = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        listed = ""
    if " libx264 " in listed:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    if "h264_videotoolbox" in listed:
        return ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-pix_fmt", "yuv420p"]
    return ["-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"]


def has_audio(ffprobe: str | None, path: str | Path) -> bool:
    if not ffprobe:
        return False
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-protocol_whitelist", "file",
                              "-select_streams", "a", "-show_entries", "stream=index",
                              "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.strip())


def media_duration(ffprobe: str | None, path: str | Path) -> float:
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-protocol_whitelist", "file",
                              "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30, check=False).stdout
        return max(0.0, float(out.strip() or 0))
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def _tools() -> tuple[str, str | None]:
    from . import media                                       # noqa: PLC0415

    ffmpeg = media.FFMPEG or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Films need ffmpeg. Install it under Settings → Extras.")
    return ffmpeg, media.FFPROBE or shutil.which("ffprobe")


def prepare(spec: dict[str, Any], ffprobe: str | None) -> dict[str, Any]:
    """*spec* with what only the files can say filled in: how long each clip
    and story is, and which clips have sound."""
    spec = dict(spec)
    items = []
    for item in spec.get("items") or []:
        item = dict(item)
        if item.get("kind") == "video":
            if not item.get("duration"):
                item["duration"] = media_duration(ffprobe, item["path"])
            item["has_audio"] = has_audio(ffprobe, item["path"])
        elif item.get("kind") == "live":
            item["duration"] = media_duration(ffprobe, item["path"])
            if not item["duration"] and item.get("still"):
                # A moving part that cannot be read: the photograph instead.
                item.update(kind="picture", path=item["still"])
        story = item.get("story")
        if story and not story.get("duration"):
            item["story"] = {**story, "duration": media_duration(ffprobe, story["path"])}
        items.append(item)
    spec["items"] = items
    return spec


def frame_count(timeline: dict[str, Any]) -> int:
    return max(1, int(math.ceil(float(timeline["duration"]) * FPS)))


def frames(timeline: dict[str, Any], items: Sequence[dict[str, Any]], ffmpeg: str,
           first_frame: int = 0, end_frame: int | None = None) -> Iterator[Image.Image]:
    """The film's frames from *first_frame* up to *end_frame* (all of them
    unless told), in order, as RGB images of its size. Every effect is worked
    out from the frame's own time, so any stretch of the film can be made on
    its own and comes out the same as it would in one go."""
    size = (int(timeline["size"][0]), int(timeline["size"][1]))
    shots = timeline["shots"]
    lettering = Lettering(size)
    total = frame_count(timeline) if end_frame is None else min(end_frame, frame_count(timeline))
    open_: dict[int, Any] = {}

    def opened(n: int) -> Any:
        if n not in open_:
            shot = shots[n]
            item = items[shot["item"]] if shot["item"] is not None else None
            if shot["kind"] in ("video", "live"):
                open_[n] = Clip(ffmpeg, item, shot, size)
            elif shot["kind"] == "pair":
                open_[n] = Pair(item, shot, size)
            else:
                open_[n] = Still(item, shot, size)
        return open_[n]

    try:
        first = 0
        for k in range(first_frame, total):
            t = k / FPS
            while first < len(shots) and shots[first]["start"] + shots[first]["dur"] <= t:
                if first in open_:
                    open_.pop(first).close()
                first += 1
            live = [n for n in range(first, min(first + 3, len(shots)))
                    if shots[n]["start"] <= t < shots[n]["start"] + shots[n]["dur"]]
            if not live:
                yield Image.new("RGB", size)
                continue
            frame = opened(live[0]).frame(t - shots[live[0]]["start"])
            if len(live) > 1:
                nxt = shots[live[1]]
                alpha = ease((t - nxt["start"]) / float(timeline.get("fade") or FADE))
                frame = Image.blend(frame, opened(live[1]).frame(t - nxt["start"]), alpha)
            frame = look(frame, timeline.get("look") or "natural")
            for n in live:
                shot = shots[n]
                if shot["kind"] in ("title", "end", "chapter") and any(shot.get("lines") or []):
                    frame = _over(frame, lettering.title(shot["lines"],
                                                         chapter=shot["kind"] == "chapter"),
                                  _fade_in_out(t, shot["start"] + 0.3,
                                               shot["start"] + shot["dur"] - 0.3, 0.6))
            for label in timeline.get("labels") or []:
                if label["start"] <= t < label["end"]:
                    frame = _over(frame, lettering.label(label["text"], label.get("at")),
                                  _fade_in_out(t, label["start"], label["end"], 0.5))
            if timeline.get("burn"):
                for sub in timeline.get("subtitles") or []:
                    if sub["start"] <= t < sub["end"]:
                        frame = _over(frame, lettering.subtitle(sub["lines"]),
                                      _fade_in_out(t, sub["start"], sub["end"], 0.15))
            if t > timeline["duration"] - 1.0:
                frame = Image.blend(frame, Image.new("RGB", size), ease(t - (timeline["duration"] - 1.0)))
            yield frame
    finally:
        for shot in open_.values():
            shot.close()


#: The shortest stretch of film given to one process of its own; shorter,
#: and starting the process costs more than it saves.
MIN_PART_SECONDS = 10.0
#: The most processes a film is made by at once.
MAX_PARTS = 8


def part_count(cpus: int | None = None) -> int:
    """How many processes make a film side by side: half the cores, as most
    eight. Each draws frames and runs its own encoder on about two cores, and
    the other half of the machine is left to the family's pages."""
    cpus = cpus or os.cpu_count() or 2
    return max(1, min(MAX_PARTS, cpus // 2))


def parts(total: int, count: int) -> list[tuple[int, int]]:
    """*total* frames cut into at most *count* runs of at least
    :data:`MIN_PART_SECONDS` each, as ``(first, end)`` pairs."""
    count = max(1, min(count, int(total // (MIN_PART_SECONDS * FPS)) or 1))
    edges = [round(total * i / count) for i in range(count + 1)]
    return [(edges[i], edges[i + 1]) for i in range(count) if edges[i + 1] > edges[i]]


def _encode_part(timeline: dict[str, Any], items: Sequence[dict[str, Any]], ffmpeg: str,
                 encoder_args: list[str], first: int, end: int, out: str,
                 progress: Any, slot: int, errors: Any) -> None:
    """One process's stretch of the film, drawn and encoded to *out*, at a
    low priority so the family's pages come first."""
    try:
        if hasattr(os, "nice"):
            os.nice(10)
    except OSError:
        pass
    try:
        w, h = timeline["size"]
        encoder = subprocess.Popen(
            [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{w}x{h}", "-r", str(FPS), "-i", "pipe:0", *encoder_args,
             "-r", str(FPS), "-an", "-f", "mp4", out],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        assert encoder.stdin is not None
        done = 0
        try:
            for frame in frames(timeline, items, ffmpeg, first, end):
                encoder.stdin.write(frame.tobytes())
                done += 1
                if done % 10 == 0:
                    progress[slot] = done
            encoder.stdin.close()
        except BrokenPipeError:
            pass
        encoder.wait()
        progress[slot] = done
        if encoder.returncode != 0:
            err = encoder.stderr.read() if encoder.stderr else b""
            errors.put(f"The film could not be encoded: {err.decode('utf-8', 'replace')[-300:]}")
    except BaseException as exc:                               # noqa: BLE001 - told to the parent
        errors.put(str(exc) or exc.__class__.__name__)


def render(spec: dict[str, Any], out_path: Path | str, *,
           report: Callable[[int, int], None] | None = None,
           cancelled: Callable[[], bool] | None = None,
           workers: int | None = None) -> dict[str, Any]:
    """Write the film *spec* describes to *out_path* as an MP4; returns its
    duration in seconds, frame count and number of shots.

    The frames are made in stretches by up to *workers* processes side by
    side (:func:`part_count` unless told), each encoding its own stretch;
    the stretches are then joined without encoding again, and the sound and
    the subtitles put with them. ``report(done, total)`` is told how many
    frames are made, and ``cancelled()`` asked as it goes: a film stopped
    halfway leaves no file behind.
    """
    ffmpeg, ffprobe = _tools()
    spec = prepare(spec, ffprobe)
    music = spec.get("music") if isinstance(spec.get("music"), dict) else None
    rough = plan(spec)
    beats = music_beats(music["path"], ffmpeg, rough["duration"] + 5) if music else []
    timeline = plan(spec, beats=beats)
    timeline["burn"] = (spec.get("subtitles") or "both") != "none"
    items = spec["items"]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = frame_count(timeline)
    runs = parts(total, workers or part_count())
    encoder_args = video_encoder(ffmpeg)
    if encoder_args[1] == "libx264":
        encoder_args += ["-threads", "2"]
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="ninaivu-film-") as scratch:
        work = Path(scratch)
        progress = ctx.Array("i", len(runs))
        errors = ctx.Queue()
        procs = []
        for slot, (first, end) in enumerate(runs):
            proc = ctx.Process(target=_encode_part,
                               args=(timeline, items, ffmpeg, encoder_args, first, end,
                                     str(work / f"part-{slot:02d}.mp4"), progress, slot, errors),
                               name=f"memory-film-part-{slot}", daemon=True)
            proc.start()
            procs.append(proc)
        sound = work / "sound.m4a"
        try:
            # The sound is mixed while the frames are drawn.
            mixed = subprocess.run(mix_command(ffmpeg, timeline, items, sound),
                                   capture_output=True, timeout=3600, check=False)
            if mixed.returncode != 0 or not sound.is_file():
                raise RuntimeError(f"The sound could not be mixed: "
                                   f"{mixed.stderr.decode('utf-8', 'replace')[-300:]}")
            while any(p.is_alive() for p in procs):
                if cancelled and cancelled():
                    raise Cancelled()
                if not errors.empty():
                    raise RuntimeError(errors.get())
                if report:
                    report(min(total, sum(progress[:])), total)
                time.sleep(0.25)
            if not errors.empty():
                raise RuntimeError(errors.get())
            failed = [p.exitcode for p in procs if p.exitcode]
            if failed:
                raise RuntimeError(f"A film process ended unexpectedly (code {failed[0]}).")
        except BaseException:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
            for proc in procs:
                proc.join(10)
            out_path.unlink(missing_ok=True)
            raise
        if report:
            report(total, total)
        listing = work / "parts.txt"
        listing.write_text("".join(f"file 'part-{slot:02d}.mp4'\n" for slot in range(len(runs))),
                           encoding="utf-8")
        tracks: list[tuple[Path, str]] = []
        languages = {str((i.get("story") or {}).get("language") or "") for i in items} - {""}
        spoken_code = "tam" if "ta" in languages else ("eng" if languages == {"en"} else "und")
        if timeline["subtitles"]:
            for line, code in ((0, spoken_code), (1, "eng")):
                text = srt(timeline["subtitles"], line)
                if text:
                    path = work / f"subtitles-{line}.srt"
                    path.write_text(text, encoding="utf-8")
                    tracks.append((path, code))
        command = [ffmpeg, "-v", "error", "-y", "-protocol_whitelist", "file",
                   "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(sound)]
        for path, _ in tracks:
            command += ["-i", str(path)]
        command += ["-map", "0:v", "-map", "1:a"]
        for n in range(len(tracks)):
            command += ["-map", f"{n + 2}:s"]
        command += ["-c:v", "copy", "-c:a", "copy"]
        if tracks:
            command += ["-c:s", "mov_text"]
            for n, (_, code) in enumerate(tracks):
                command += [f"-metadata:s:s:{n}", f"language={code}"]
        title = str(spec.get("title") or "")[:200]
        command += ["-metadata", f"title={title}", "-movflags", "+faststart",
                    "-t", f"{timeline['duration']:.3f}", "-f", "mp4", str(out_path)]
        joined = subprocess.run(command, capture_output=True, timeout=3600, check=False)
        if joined.returncode != 0:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(f"The film could not be put together: "
                               f"{joined.stderr.decode('utf-8', 'replace')[-300:]}")
    return {"seconds": timeline["duration"], "frames": total, "shots": len(timeline["shots"]),
            "parts": len(runs)}


# ---------------------------------------------------------------------------
# Making a film in the background
# ---------------------------------------------------------------------------

#: Films made at once, across the whole server: one. A film keeps the
#: processor busy for minutes, and a second would only make both slower.
MAX_BUILDING = 1

_live: dict[int, dict[str, Any]] = {}
_live_lock = threading.Lock()


class Busy(RuntimeError):
    """Another film is being made."""


def building() -> bool:
    with _live_lock:
        return any(job["state"] == "building" for job in _live.values())


def progress(film_id: int) -> dict[str, Any] | None:
    with _live_lock:
        job = _live.get(int(film_id))
        if job is None:
            return None
        return {"done": job["done"], "total": job["total"], "state": job["state"]}


def cancel(film_id: int) -> bool:
    with _live_lock:
        job = _live.get(int(film_id))
        if job is None or job["state"] != "building":
            return False
        job["cancel"].set()
        return True


def start(film_id: int, spec: dict[str, Any], *, db_path: Path | str,
          state_dir: Path | str) -> None:
    """Make film *film_id* in the background, recording how it ended.

    Busy if a film is already being made. The frames are drawn by processes
    of their own at a low priority (:func:`render`); this thread only waits on
    them and on ffmpeg. The MP4 is written under a temporary name and renamed
    into place, so a download can never start on half a film.
    """
    from ..storage import db, films as store                  # noqa: PLC0415

    film_id = int(film_id)
    with _live_lock:
        if any(job["state"] == "building" for job in _live.values()):
            raise Busy("A film is already being made. Try again when it is finished.")
        stop = threading.Event()
        job = {"done": 0, "total": 0, "state": "building", "cancel": stop}
        _live[film_id] = job

    folder = store.films_dir(state_dir)
    name = f"film-{film_id}-{os.urandom(6).hex()}.mp4"
    part = folder / (name + ".part")

    def report(done: int, total: int) -> None:
        with _live_lock:
            job.update(done=done, total=total)

    def run() -> None:
        outcome, error, seconds = "error", "", 0.0
        try:
            folder.mkdir(parents=True, exist_ok=True)
            info = render(spec, part, report=report, cancelled=stop.is_set)
            os.replace(part, folder / name)
            outcome, seconds = "done", float(info["seconds"])
        except Cancelled:
            outcome = "cancelled"
        except Exception as exc:                               # noqa: BLE001 - recorded, not raised
            log.exception("film %s could not be made", film_id)
            error = str(exc) or exc.__class__.__name__
        finally:
            part.unlink(missing_ok=True)
        try:
            conn = db.connect(db_path)
            if outcome == "done":
                path = folder / name
                if store.get(conn, film_id) is None:
                    path.unlink(missing_ok=True)       # deleted while it was being made
                else:
                    store.set_state(conn, film_id, "done", file=name,
                                    size_bytes=path.stat().st_size, seconds=seconds)
            elif store.get(conn, film_id) is not None:
                store.set_state(conn, film_id, outcome, error=error)
        except Exception:                                      # noqa: BLE001
            log.exception("film %s: its state could not be recorded", film_id)
        finally:
            db.close_all()
            with _live_lock:
                job["state"] = outcome
                for old in [k for k, j in _live.items() if j["state"] != "building"][:-8]:
                    _live.pop(old, None)

    threading.Thread(target=run, name=f"memory-film-{film_id}", daemon=True).start()


def wait(film_id: int, timeout: float = 120.0) -> str:
    """Until a film is no longer being made; for tests and the command line."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = (progress(film_id) or {}).get("state")
        if state != "building":
            return state or ""
        time.sleep(0.1)
    return "building"


def reset() -> None:
    """Forget every film being made — for tests."""
    with _live_lock:
        _live.clear()


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m ninaivu.media.films spec.json out.mp4``: make a film from a
    spec written by hand or by the API, for trying the renderer on its own."""
    import sys                                                # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m ninaivu.media.films SPEC.json OUT.mp4", file=sys.stderr)
        return 2
    spec = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    began = time.monotonic()

    def report(done: int, total: int) -> None:
        print(f"\r{done}/{total} frames", end="", file=sys.stderr, flush=True)

    info = render(spec, args[1], report=report)
    print(f"\n{info['seconds']:.1f} s of film, {info['frames']} frames, {info['shots']} shots, "
          f"{info['parts']} processes, made in {time.monotonic() - began:.1f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
