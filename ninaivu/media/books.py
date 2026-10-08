"""Photo books for occasions: a wedding, Pongal, a sixtieth birthday, a holiday.

A family that wants the wedding printed does not want a ZIP of four hundred
photographs. It wants thirty-six good ones, in order, laid out on pages, in a
file any print shop will take. This makes that file.

Three steps, and only the first one looks at the library:

**Picking** (:func:`pick`) is a pure function over rows the caller already
fetched *through the viewer's own limits* — the same query every listing
runs — so a book can never hold a photograph its maker could not have found
by scrolling. It drops what nobody prints (a blurred frame, a screenshot, the
fourth copy of the same pose), prefers faces, favourites and sharp, large
pictures, and spreads what it keeps across the occasion so the whole day is
in the book and not only the hour when the camera was busiest.

**Planning** (:func:`plan_pages`) puts the picks on pages of one to four
frames, choosing for each run of photographs the arrangement whose frames
their shapes fill best: two portraits side by side, two landscapes one above
the other, a photograph that stands out on a page of its own.

**Rendering** (:func:`render_book`) composes each page at 300 dpi with Pillow
and writes it straight into the PDF before the next is started. A page of A4
at 300 dpi is 26 MB of pixels; holding a forty-page book in memory to hand to
``Image.save(save_all=True)`` would be a gigabyte, which a 2 GB Raspberry Pi
does not have to spare. So the PDF is written here, by hand — it is a few
dozen lines for pages that are one JPEG each — and memory stays at one page
and one photograph however long the book is. Photographs are decoded only as
large as their frame (a JPEG straight from its reduced-size decode), turned
upright the way the gallery shows them, and the page is JPEG-compressed.

Text is the hard part, because a family's book is often in Tamil. Pillow
here shapes Tamil with raqm, but it needs a font that has the letters, and
Ninaivu ships none outside the Windows build. :func:`fonts` looks for one
the machine already has (Noto, Lohit, FreeSerif, Tamil Sangam MN, Nirmala…)
and every string is drawn in runs, Tamil letters in the Tamil font and the
rest in the Latin one. With no Tamil font at all the Tamil words are left
out rather than printed as boxes, and the page that builds the book says so
before anybody presses Build.

Nothing here talks to the network. The book is built on this machine and
stays in the state folder until its owner downloads or deletes it.
"""

from __future__ import annotations

import bisect
import functools
import io
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sizes and templates
# ---------------------------------------------------------------------------

DPI = 300
#: What print shops ask for: the background runs this far past the cut.
BLEED_MM = 3.0

#: Trim sizes in millimetres, width × height.
SIZES: dict[str, tuple[float, float]] = {
    "a4-portrait": (210.0, 297.0),
    "a4-landscape": (297.0, 210.0),
    "square-20": (200.0, 200.0),
    "square-30": (300.0, 300.0),
}

#: Accent, a second colour for the ornament, and the paper. Kept quiet: the
#: photographs are the colour in a book; the template is a frame around them.
TEMPLATES: dict[str, dict[str, Any]] = {
    "plain":     {"accent": (70, 74, 82), "second": (160, 164, 170), "paper": (255, 255, 255)},
    "wedding":   {"accent": (128, 24, 48), "second": (196, 152, 64), "paper": (255, 252, 246)},
    "pongal":    {"accent": (46, 110, 52), "second": (214, 132, 26), "paper": (255, 253, 245)},
    "deepavali": {"accent": (176, 74, 0), "second": (222, 172, 40), "paper": (255, 250, 242)},
    "birthday":  {"accent": (52, 96, 156), "second": (226, 96, 120), "paper": (255, 255, 255)},
    "holiday":   {"accent": (0, 112, 120), "second": (240, 170, 60), "paper": (252, 255, 255)},
}

DEFAULT_COUNT = 36
MIN_COUNT = 12
MAX_COUNT = 120

#: JPEG quality of a page. Chroma is kept whole (4:4:4) so the coloured
#: ornament and the small caption type stay crisp at the print shop.
PAGE_QUALITY = 86

_MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")


def mm_px(mm: float) -> int:
    return int(round(mm * DPI / 25.4))


def page_pixels(size: str, bleed: bool = False) -> tuple[int, int, int]:
    """(width, height, bleed) in pixels at 300 dpi for a size, bleed included.

    The bleed is added to the trim on each side, so the trim inside is always
    exactly the size asked for whether or not there is bleed around it.
    """
    if size not in SIZES:
        raise ValueError(f"unknown size: {size}")
    w_mm, h_mm = SIZES[size]
    extra = mm_px(BLEED_MM) if bleed else 0
    return mm_px(w_mm) + 2 * extra, mm_px(h_mm) + 2 * extra, extra


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_TAMIL_SAMPLE = "தமிழ்"
#: A code point no font maps, to learn what "no glyph" looks like in one.
_UNMAPPED = "\U0010FFFD"


def _font_dirs() -> list[Path]:
    dirs = [Path(__file__).resolve().parent.parent / "static" / "fonts"]
    home = Path.home()
    if sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts/Supplemental"), Path("/System/Library/Fonts"),
                 Path("/Library/Fonts"), home / "Library" / "Fonts"]
    elif os.name == "nt":
        dirs += [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                 home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 home / ".local" / "share" / "fonts", home / ".fonts"]
    return dirs


#: File names worth trying, best first. The bundled Windows font leads; then
#: the Noto faces the guides are printed with, then whatever families the
#: common systems carry. Searched for under the font folders, not at fixed
#: paths: Debian, Fedora and Arch all file them differently.
_TAMIL_NAMES = (
    ("NotoSerifTamil-Regular.ttf", "NotoSerifTamil-Bold.ttf"),
    ("NotoSansTamil-Regular.ttf", "NotoSansTamil-Bold.ttf"),
    ("NotoSansTamil.ttf", None),
    ("NotoSerifTamil[wght].ttf", None),
    ("NotoSansTamil[wdth,wght].ttf", None),
    ("Tamil Sangam MN.ttc", None),
    ("TamilSangamMN.ttc", None),
    ("Nirmala.ttf", "NirmalaB.ttf"),
    ("Nirmala.ttc", None),
    ("latha.ttf", "lathab.ttf"),
    ("Lohit-Tamil.ttf", None),
    ("Lohit-Tamil-Classical.ttf", None),
    ("Samyak-Tamil.ttf", None),
    ("FreeSerif.ttf", "FreeSerifBold.ttf"),
    ("FreeSans.ttf", "FreeSansBold.ttf"),
)
_LATIN_NAMES = (
    ("NotoSerif-Regular.ttf", "NotoSerif-Bold.ttf"),
    ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf"),
    ("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf"),
    ("Georgia.ttf", "Georgia Bold.ttf"),
    ("georgia.ttf", "georgiab.ttf"),
    ("FreeSerif.ttf", "FreeSerifBold.ttf"),
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
)


def _find(name: str, dirs: Sequence[Path]) -> Path | None:
    for folder in dirs:
        if not folder.is_dir():
            continue
        direct = folder / name
        if direct.is_file():
            return direct
        try:
            for found in folder.rglob(name):
                if found.is_file():
                    return found
        except OSError:
            continue
    return None


def covers(path: str | Path, text: str) -> bool:
    """Whether the font at *path* has a glyph for every letter of *text*.

    Pillow does not expose a font's character map, so each letter is drawn
    and compared with what the font draws for a code point nobody maps: a
    letter it lacks comes out as that same "missing" glyph (a box, or
    nothing).
    """
    try:
        font = ImageFont.truetype(str(path), 48)

        def drawn(ch: str) -> bytes:
            image = Image.new("L", (96, 96))
            ImageDraw.Draw(image).text((20, 20), ch, font=font, fill=255)
            return image.tobytes()

        missing = drawn(_UNMAPPED)
        for ch in set(text):
            if ch.isspace() or ch in "\u200c\u200d" or not ch.isprintable():
                continue
            if drawn(ch) == missing:
                return False
        return True
    except (OSError, ValueError):
        return False


def _fc_list(lang: str) -> list[Path]:
    """What fontconfig says covers a language, where there is fontconfig."""
    tool = shutil.which("fc-list")
    if not tool:
        return []
    try:
        out = subprocess.run([tool, f":lang={lang}", "file"], capture_output=True,
                             text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        path = line.split(":")[0].strip()
        # Unifont is a bitmap face drawn on a 16-pixel grid: it covers every
        # script and looks right in none of them at 300 dpi.
        if path and "unifont" not in path.lower() and path.lower().endswith((".ttf", ".otf", ".ttc")):
            found.append(Path(path))
    return sorted(found)


@functools.lru_cache(maxsize=1)
def fonts() -> dict[str, str]:
    """The fonts a book is set in: Latin and Tamil, regular and bold.

    An empty string where there is none. Looked up once a process; a font
    installed later is picked up at the next start.
    """
    dirs = _font_dirs()
    chosen = {"latin": "", "latin_bold": "", "tamil": "", "tamil_bold": ""}
    for regular, bold in _LATIN_NAMES:
        path = _find(regular, dirs)
        if path and covers(path, "Aa"):
            chosen["latin"] = str(path)
            bold_path = _find(bold, dirs) if bold else None
            chosen["latin_bold"] = str(bold_path or path)
            break
    candidates: list[tuple[Path, Path | None]] = []
    for regular, bold in _TAMIL_NAMES:
        path = _find(regular, dirs)
        if path:
            candidates.append((path, _find(bold, dirs) if bold else None))
    candidates += [(p, None) for p in _fc_list("ta")]
    for path, bold_path in candidates:
        if covers(path, _TAMIL_SAMPLE):
            chosen["tamil"] = str(path)
            chosen["tamil_bold"] = str(bold_path if bold_path and covers(bold_path, _TAMIL_SAMPLE)
                                       else path)
            break
    return chosen


def font_report() -> dict[str, Any]:
    """What the page building a book needs to know before Build is pressed."""
    found = fonts()
    return {"tamil": bool(found["tamil"]), "latin": bool(found["latin"]),
            "tamil_font": Path(found["tamil"]).name if found["tamil"] else "",
            "latin_font": Path(found["latin"]).name if found["latin"] else ""}


def _is_tamil(ch: str) -> bool:
    return "\u0b80" <= ch <= "\u0bff"


def text_runs(text: str) -> list[tuple[bool, str]]:
    """*text* cut into runs of Tamil and of everything else.

    Spaces and the joiners Tamil spelling uses stay with the run they are in,
    so a Tamil phrase is shaped as one piece; punctuation and digits go to
    the Latin font, which certainly has them.
    """
    runs: list[tuple[bool, str]] = []
    for ch in text:
        if _is_tamil(ch):
            tamil = True
        elif ch in " \u200c\u200d" and runs:
            tamil = runs[-1][0]
        else:
            tamil = False
        if runs and runs[-1][0] == tamil:
            runs[-1] = (tamil, runs[-1][1] + ch)
        else:
            runs.append((tamil, ch))
    return runs


class Type:
    """Sets text in the book's fonts, a run at a time."""

    def __init__(self, found: dict[str, str] | None = None) -> None:
        self.found = found if found is not None else fonts()
        self._cache: dict[tuple[int, bool, bool], Any] = {}

    def font(self, size: int, *, bold: bool = False, tamil: bool = False):  # noqa: ANN201
        key = (int(size), bold, tamil)
        if key not in self._cache:
            if tamil:
                path = self.found.get("tamil_bold" if bold else "tamil") or ""
                if not path:
                    self._cache[key] = None
                    return None
            else:
                path = self.found.get("latin_bold" if bold else "latin") or ""
            try:
                self._cache[key] = (ImageFont.truetype(path, int(size)) if path
                                    else ImageFont.load_default(int(size)))
            except OSError:
                self._cache[key] = ImageFont.load_default(int(size))
        return self._cache[key]

    def printable(self, text: str) -> str:
        """*text* as it will print: without the Tamil, when there is no font for it."""
        if self.found.get("tamil") or not any(_is_tamil(c) for c in text):
            return text
        kept = "".join(seg for tamil, seg in text_runs(text) if not tamil)
        return " ".join(kept.split()).strip(" ,·-–—")

    def _pieces(self, text: str, size: int, bold: bool):  # noqa: ANN202
        for tamil, segment in text_runs(self.printable(text)):
            font = self.font(size, bold=bold, tamil=tamil)
            if font is not None:
                yield tamil, segment, font

    def width(self, text: str, size: int, *, bold: bool = False) -> int:
        total = 0.0
        for tamil, segment, font in self._pieces(text, size, bold):
            total += font.getlength(segment, **({"language": "ta"} if tamil else {}))
        return int(math.ceil(total))

    def draw(self, draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str,
             size: int, fill: tuple[int, int, int], *, bold: bool = False,
             align: str = "left") -> int:
        """Draw *text* on a baseline; returns its width. ``align`` is where *x* is."""
        width = self.width(text, size, bold=bold)
        if align == "center":
            x -= width / 2
        elif align == "right":
            x -= width
        for tamil, segment, font in self._pieces(text, size, bold):
            extra = {"language": "ta"} if tamil else {}
            try:
                draw.text((x, baseline), segment, font=font, fill=fill, anchor="ls", **extra)
            except (ValueError, TypeError):
                # The tiny bitmap default cannot take an anchor.
                draw.text((x, baseline - size), segment, font=font, fill=fill)
            x += font.getlength(segment, **extra)
        return width

    def fit(self, text: str, size: int, max_width: int, *, bold: bool = False,
            smallest: int = 10) -> int:
        """The largest size up to *size* at which *text* fits on one line."""
        while size > smallest and self.width(text, size, bold=bold) > max_width:
            size = max(smallest, int(size * 0.92))
        return size

    def wrap(self, text: str, size: int, max_width: int, max_lines: int, *,
             bold: bool = False) -> list[str]:
        """*text* broken into at most *max_lines* lines, the last cut with an ellipsis."""
        words = self.printable(text).split()
        lines: list[str] = []
        line = ""
        for word in words:
            trial = f"{line} {word}".strip()
            if self.width(trial, size, bold=bold) <= max_width or not line:
                line = trial
                continue
            lines.append(line)
            line = word
            if len(lines) == max_lines:
                break
        if line and len(lines) < max_lines:
            lines.append(line)
        elif line and lines:
            last = lines[-1]
            while last and self.width(last + "…", size, bold=bold) > max_width:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
        return lines


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

def _field(row: Any, name: str, default: Any = None) -> Any:
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _flags(row: Any) -> list[str]:
    raw = _field(row, "quality", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    return [str(f) for f in raw or []]


def when(row: Any) -> float:
    return float(_field(row, "captured_at") or _field(row, "mtime") or 0.0)


def score(row: Any, faces: int = 0) -> float:
    """How much a photograph wants to be in the book. Only ever compared.

    Faces count most — a family book is a book of people — then what the
    household has said it likes, then focus and size. Measurements that are
    missing add nothing; they do not count against a photograph.
    """
    value = 1.0
    if faces:
        value += 0.6 + 0.15 * min(int(faces), 4)
    if _field(row, "favorite"):
        value += 0.8
    value += 0.2 * int(_field(row, "rating", 0) or 0)
    sharpness = _field(row, "sharpness")
    if sharpness is not None:
        value += min(0.5, math.log10(1.0 + max(0.0, float(sharpness))) / 6.0)
    width, height = int(_field(row, "width", 0) or 0), int(_field(row, "height", 0) or 0)
    if width and height:
        value += min(0.4, width * height / 30e6)
    flags = _flags(row)
    value -= 0.3 * sum(1 for f in ("dark", "bright", "clipped") if f in flags)
    if "lowres" in flags:
        value -= 0.6
    return round(value, 4)


def _hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


#: Perceptual hashes this close are the same picture taken twice.
NEAR_DUPLICATE_BITS = 6
#: How many neighbours in time a photograph is compared with. A burst is
#: next to itself; the same pose an hour later is a different moment.
NEAR_WINDOW = 6


def pick(rows: Sequence[Any], count: int = DEFAULT_COUNT, *,
         screens: Iterable[int] = (), faces: dict[int, int] | None = None,
         ) -> list[dict[str, Any]]:
    """The photographs for a book, oldest first.

    ``rows`` are what the viewer may see of the source, already limited by the
    caller. ``screens`` are the ids judged screenshots or documents; ``faces``
    the number of faces found in each id. Each returned item is the row as a
    dict with ``score`` and ``hero`` (worth a page of its own) added.
    """
    count = max(1, int(count))
    faces = faces or {}
    screens = set(screens)
    usable = []
    for row in rows:
        if _field(row, "kind", "picture") != "picture" or int(_field(row, "id")) in screens:
            continue
        if "blurry" in _flags(row):
            continue
        item = dict(row)
        item["score"] = score(row, faces.get(int(item["id"]), 0))
        usable.append(item)
    usable.sort(key=lambda r: (when(r), int(r["id"])))

    # The same photograph twice — a copy, an edit beside its original — is
    # one group; the best of it stays.
    best_of_group: dict[str, dict[str, Any]] = {}
    singles = []
    for item in usable:
        group = item.get("dup_group")
        if not group:
            singles.append(item)
        elif group not in best_of_group or item["score"] > best_of_group[group]["score"]:
            best_of_group[group] = item
    usable = sorted([*singles, *best_of_group.values()], key=lambda r: (when(r), int(r["id"])))

    # A burst, or the same pose taken three times: near in time and near in
    # what it looks like. The better frame takes the place.
    kept: list[dict[str, Any]] = []
    for item in usable:
        phash = item.get("phash")
        twin = None
        if phash:
            for index in range(len(kept) - 1, max(-1, len(kept) - 1 - NEAR_WINDOW), -1):
                other = kept[index].get("phash")
                if other and _hamming(phash, other) <= NEAR_DUPLICATE_BITS:
                    twin = index
                    break
        if twin is None:
            kept.append(item)
        elif item["score"] > kept[twin]["score"]:
            kept[twin] = item

    if len(kept) > count:
        kept = _spread(kept, count)
    return mark_heroes(kept)


def mark_heroes(items: list[dict[str, Any]], faces: dict[int, int] | None = None,
                ) -> list[dict[str, Any]]:
    """*items* oldest first, each scored and told whether it is a hero.

    About one in six gets a page of its own — the best ones, but never so many
    that the book is a slideshow. Used on its own for the photographs a person
    has already chosen, which are not to be thinned again.
    """
    for item in items:
        if "score" not in item:
            item["score"] = score(item, (faces or {}).get(int(item["id"]), 0))
    items.sort(key=lambda r: (when(r), int(r["id"])))
    heroes = max(1, len(items) // 6) if len(items) >= 4 else 0
    ranked = sorted(items, key=lambda r: -r["score"])
    hero_ids = {int(r["id"]) for r in ranked[:heroes]}
    for item in items:
        item["hero"] = int(item["id"]) in hero_ids
    return items


def _spread(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """The best *count*, but not all from the busiest hour.

    Taken best first, each one only if nothing chosen is closer to it in time
    than a fair share of the span; whatever places are left are then filled
    best first regardless. A wedding's three hundred photographs of the
    ceremony leave room for the morning and the evening.
    """
    times = [when(i) for i in items]
    span = max(times) - min(times)
    gap = span / (count * 1.5) if span > 0 else 0.0
    order = sorted(range(len(items)), key=lambda i: -items[i]["score"])
    chosen: list[int] = []
    taken: list[float] = []
    for index in order:
        if len(chosen) >= count:
            break
        t = times[index]
        at = bisect.bisect_left(taken, t)
        near = [abs(taken[j] - t) for j in (at - 1, at) if 0 <= j < len(taken)]
        if gap and near and min(near) < gap:
            continue
        chosen.append(index)
        taken.insert(at, t)
    if len(chosen) < count:
        picked = set(chosen)
        chosen += [i for i in order if i not in picked][:count - len(chosen)]
    return [items[i] for i in chosen]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

Rect = tuple[int, int, int, int]

#: How many frames each arrangement has, and how much it is preferred when
#: its frames fit equally well: a page of two reads better than a page of
#: four small ones.
LAYOUTS = {"2v": 2, "2h": 2, "3t": 3, "3l": 3, "4": 4}
_WEIGHT = {1: 1.0, 2: 1.0, 3: 0.985, 4: 0.96}


class Geometry:
    """Where things go on a page of one size."""

    def __init__(self, size: str, bleed: bool = False, captions: bool = False) -> None:
        self.size = size
        self.width, self.height, self.bleed = page_pixels(size, bleed)
        self.trim_w = self.width - 2 * self.bleed
        self.trim_h = self.height - 2 * self.bleed
        short = min(self.trim_w, self.trim_h)
        self.margin = int(short * 0.075)
        self.gutter = int(short * 0.022)
        self.captions = captions
        self.caption_size = max(24, int(short * 0.0145))
        self.caption_h = int(self.caption_size * 3.4) if captions else 0

    @property
    def content(self) -> Rect:
        b, m = self.bleed, self.margin
        # A little more room at the foot, where the page number sits.
        return (b + m, b + m, b + self.trim_w - m, b + self.trim_h - int(m * 1.15))

    def frames(self, layout: str) -> list[Rect]:
        """The frames of an arrangement, in reading order, captions left out."""
        x0, y0, x1, y1 = self.content
        g = self.gutter
        midx, midy = (x0 + x1) // 2, (y0 + y1) // 2
        if layout == "1":
            cells = [(x0, y0, x1, y1)]
        elif layout == "2v":
            cells = [(x0, y0, midx - g // 2, y1), (midx + g // 2, y0, x1, y1)]
        elif layout == "2h":
            cells = [(x0, y0, x1, midy - g // 2), (x0, midy + g // 2, x1, y1)]
        elif layout == "3t":
            cells = [(x0, y0, x1, midy - g // 2), (x0, midy + g // 2, midx - g // 2, y1),
                     (midx + g // 2, midy + g // 2, x1, y1)]
        elif layout == "3l":
            cells = [(x0, y0, midx - g // 2, y1), (midx + g // 2, y0, x1, midy - g // 2),
                     (midx + g // 2, midy + g // 2, x1, y1)]
        elif layout == "4":
            cells = [(x0, y0, midx - g // 2, midy - g // 2), (midx + g // 2, y0, x1, midy - g // 2),
                     (x0, midy + g // 2, midx - g // 2, y1), (midx + g // 2, midy + g // 2, x1, y1)]
        else:
            raise ValueError(layout)
        return [(a, b, c, d - self.caption_h) for a, b, c, d in cells]


def aspect(item: Any) -> float:
    """Width over height as the gallery shows it (the index stores it upright)."""
    w, h = int(_field(item, "width", 0) or 0), int(_field(item, "height", 0) or 0)
    return w / h if w > 0 and h > 0 else 1.5


def _fill(frame: Rect, ratio: float) -> float:
    fw, fh = frame[2] - frame[0], frame[3] - frame[1]
    if fw <= 0 or fh <= 0:
        return 0.0
    f = fw / fh
    return min(f / ratio, ratio / f)


def plan_pages(items: Sequence[Any], geometry: Geometry) -> list[dict[str, Any]]:
    """Put the photographs, in order, on pages.

    A hero has a page to itself. Otherwise the next two, three or four go on
    whichever arrangement their shapes fill best, a little in favour of a
    change from the page before. A hero is never folded into a page of
    smaller pictures.
    """
    pages: list[dict[str, Any]] = []
    i, previous = 0, ""
    items = list(items)
    while i < len(items):
        if _field(items[i], "hero", False):
            pages.append({"layout": "1", "items": [items[i]]})
            i, previous = i + 1, "1"
            continue
        best, best_value = "1", _fill(geometry.frames("1")[0], aspect(items[i])) * 0.8
        for layout, k in LAYOUTS.items():
            group = items[i:i + k]
            if len(group) < k or any(_field(g, "hero", False) for g in group):
                continue
            fits = [_fill(f, aspect(g)) for f, g in zip(geometry.frames(layout), group)]
            value = (sum(fits) / k) * _WEIGHT[k] - (0.04 if layout == previous else 0.0)
            if value > best_value + 1e-9:
                best, best_value = layout, value
        k = 1 if best == "1" else LAYOUTS[best]
        pages.append({"layout": best, "items": items[i:i + k]})
        i, previous = i + k, best
    return pages


# ---------------------------------------------------------------------------
# Ornaments — drawn, so a template needs no image files
# ---------------------------------------------------------------------------

def ornament(draw: ImageDraw.ImageDraw, template: str, cx: int, cy: int, unit: int) -> None:
    """A small device centred on (cx, cy), *unit* pixels to a step."""
    style = TEMPLATES.get(template, TEMPLATES["plain"])
    a, s = style["accent"], style["second"]
    line = max(2, unit // 6)
    if template == "wedding":
        # Two rings, interlaced, and a rule either side.
        r = unit * 2
        draw.ellipse((cx - r * 1.6, cy - r, cx + r * 0.4, cy + r), outline=s, width=line)
        draw.ellipse((cx - r * 0.4, cy - r, cx + r * 1.6, cy + r), outline=a, width=line)
        for side in (-1, 1):
            draw.line((cx + side * r * 2.4, cy, cx + side * r * 7, cy), fill=s, width=max(1, line // 2))
            d = unit * 0.6
            x = cx + side * r * 7.6
            draw.polygon([(x, cy - d), (x + d, cy), (x, cy + d), (x - d, cy)], fill=a)
    elif template == "pongal":
        # A kolam: a grid of dots with the lines that loop through them.
        step = unit * 1.6
        for gx in range(-2, 3):
            for gy in range(-1, 2):
                x, y = cx + gx * step, cy + gy * step
                rr = unit * 0.22
                draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=s)
        for gx in range(-2, 2):
            x = cx + (gx + 0.5) * step
            draw.ellipse((x - step * 0.5, cy - step * 0.5, x + step * 0.5, cy + step * 0.5),
                         outline=a, width=line)
        draw.line((cx - step * 3.4, cy, cx - step * 2.6, cy), fill=a, width=line)
        draw.line((cx + step * 2.6, cy, cx + step * 3.4, cy), fill=a, width=line)
    elif template == "deepavali":
        # Three lamps: a bowl, and a flame above it.
        for k in (-1, 0, 1):
            x = cx + k * unit * 5
            w = unit * (1.8 if k == 0 else 1.4)
            draw.chord((x - w, cy - w * 0.7, x + w, cy + w * 0.7), 0, 180, fill=a)
            fh = w * 1.3
            draw.polygon([(x, cy - fh), (x + w * 0.35, cy - fh * 0.35), (x, cy - w * 0.05),
                          (x - w * 0.35, cy - fh * 0.35)], fill=s)
        for side in (-1, 1):
            draw.line((cx + side * unit * 7.5, cy + unit * 0.4, cx + side * unit * 12, cy + unit * 0.4),
                      fill=s, width=max(1, line // 2))
    elif template == "birthday":
        # Balloons on strings.
        for k, colour in ((-1, s), (0, a), (1, s)):
            x = cx + k * unit * 2.6
            top = cy - unit * (2.8 if k == 0 else 2.2)
            w, h = unit * 1.2, unit * 1.5
            draw.ellipse((x - w, top - h, x + w, top + h), fill=colour)
            draw.line((x, top + h, x + k * unit * 0.6, cy + unit * 2.4), fill=a, width=max(1, line // 2))
    elif template == "holiday":
        # The sun on the sea.
        r = unit * 1.6
        draw.ellipse((cx - r, cy - r * 1.6, cx + r, cy + r * 0.4), fill=s)
        for k in range(-3, 3):
            x = cx + k * unit * 2.2
            draw.arc((x, cy - unit * 0.6, x + unit * 2.2, cy + unit * 0.9), 180, 360, fill=a, width=line)
    else:
        # Plain: a short rule with a dot in the middle.
        draw.line((cx - unit * 6, cy, cx + unit * 6, cy), fill=s, width=max(1, line // 2))
        rr = unit * 0.45
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=a)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _open_photo(item: dict[str, Any], box: tuple[int, int]) -> Image.Image:
    """The photograph upright, as the gallery shows it, fitted inside *box*.

    Decoded only as large as the frame needs (a JPEG straight from a reduced
    decode), turned by its EXIF tag and then by the index's own correction —
    the same turn the thumbnails are made with. Ninaivu keeps no
    non-destructive edits (an edit is saved as a new file beside the
    original), so the file is the photograph.
    """
    from . import media, upright                              # noqa: PLC0415

    image, _ = media.open_for_index(item["path"], max(box))
    try:
        turned = upright.apply(image, int(item.get("rotation") or 0))
        if turned.mode != "RGB":
            turned = turned.convert("RGB")
        scale = min(box[0] / turned.width, box[1] / turned.height)
        size = (max(1, round(turned.width * scale)), max(1, round(turned.height * scale)))
        if size != turned.size:
            turned = turned.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        return turned
    finally:
        image.close()


def _date_text(ts: float, months: Sequence[str]) -> str:
    if not ts:
        return ""
    try:
        from ..archive.dates import from_timestamp                # noqa: PLC0415
        day = from_timestamp(ts)
    except (OverflowError, OSError, ValueError, ImportError):
        try:
            day = datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return ""
    return f"{day.day} {months[day.month - 1]} {day.year}"


class Book:
    """Draws the pages of one book. Holds no page longer than it takes to draw it."""

    def __init__(self, spec: dict[str, Any], type_: Type | None = None) -> None:
        self.spec = spec
        self.template = spec.get("template") if spec.get("template") in TEMPLATES else "plain"
        self.style = TEMPLATES[self.template]
        self.geo = Geometry(spec.get("size", "a4-portrait"), bool(spec.get("bleed")),
                            bool(spec.get("captions")))
        self.type = type_ or Type()
        months = spec.get("months")
        self.months = (list(months) if isinstance(months, (list, tuple)) and len(months) == 12
                       else list(_MONTHS_EN))
        self.items = list(spec.get("items") or [])
        self.pages = plan_pages(self.items, self.geo)
        self.failed = 0

    @property
    def page_count(self) -> int:
        return len(self.pages) + 2                           # the cover and the closing page

    def _canvas(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        page = Image.new("RGB", (self.geo.width, self.geo.height), self.style["paper"])
        return page, ImageDraw.Draw(page)

    def _place(self, page: Image.Image, item: dict[str, Any], frame: Rect,
               valign: str = "center") -> Rect:
        fw, fh = frame[2] - frame[0], frame[3] - frame[1]
        try:
            photo = _open_photo(item, (fw, fh))
        except Exception as exc:                               # noqa: BLE001 - one bad file is one blank frame
            log.warning("book: photograph %s could not be drawn: %s", item.get("id"), exc)
            self.failed += 1
            ratio = aspect(item)
            scale = min(fw / ratio, fh)
            w, h = int(scale * ratio), int(scale)
            x, y = frame[0] + (fw - w) // 2, frame[1] + (fh - h) // 2
            ImageDraw.Draw(page).rectangle((x, y, x + w, y + h), fill=(232, 232, 232))
            return (x, y, x + w, y + h)
        x = frame[0] + (fw - photo.width) // 2
        y = frame[1] + ((fh - photo.height) // 2 if valign == "center" else fh - photo.height)
        page.paste(photo, (x, y))
        box = (x, y, x + photo.width, y + photo.height)
        photo.close()
        return box

    def _caption(self, draw: ImageDraw.ImageDraw, item: dict[str, Any], box: Rect) -> None:
        size = self.geo.caption_size
        width = max(10, box[2] - box[0])
        first = (item.get("caption") or "").strip()
        second = " · ".join(p for p in (_date_text(float(item.get("when") or 0), self.months),
                                       ", ".join(item.get("names") or [])) if p)
        y = box[3] + int(size * 1.35)
        for text, colour in ((first, (60, 60, 64)), (second, self.style["accent"])):
            if not text:
                continue
            line = self.type.wrap(text, size, width, 1)
            if line:
                self.type.draw(draw, box[0], y, line[0], size, colour)
                y += int(size * 1.3)

    def _folio(self, draw: ImageDraw.ImageDraw, number: int) -> None:
        g = self.geo
        size = max(22, int(g.caption_size * 0.95))
        y = g.bleed + g.trim_h - int(g.margin * 0.55)
        self.type.draw(draw, g.bleed + g.trim_w / 2, y, str(number), size,
                       self.style["second"], align="center")

    def cover(self) -> Image.Image:
        page, draw = self._canvas()
        g = self.geo
        x0, y0 = g.bleed + g.margin, g.bleed + g.margin
        x1 = g.bleed + g.trim_w - g.margin
        y1 = g.bleed + g.trim_h - g.margin
        short = min(g.trim_w, g.trim_h)
        title = self.spec.get("title") or ""
        subtitle = self.spec.get("subtitle") or ""
        title_size = int(short * 0.062)
        sub_size = int(short * 0.026)
        # Every ornament fits within five units either side of its centre;
        # a unit more keeps it off the photograph.
        unit = max(6, int(short * 0.013))
        text_h = int(title_size * 2.9 + sub_size * 2.2 + unit * 12)
        cover = next((i for i in self.items if i.get("id") == self.spec.get("cover_id")), None)
        cover = cover or (max(self.items, key=lambda i: i.get("score", 0)) if self.items else None)
        if cover is not None:
            self._place(page, cover, (x0, y0, x1, y1 - text_h), valign="bottom")
        oy = y1 - text_h + unit * 6
        ornament(draw, self.template, (x0 + x1) // 2, oy, unit)
        width = x1 - x0
        lines = self.type.wrap(title, title_size, width, 2, bold=True)
        if len(lines) == 1:
            title_size = self.type.fit(lines[0], int(title_size * 1.15), width, bold=True)
        y = oy + unit * 4 + title_size
        for line in lines:
            self.type.draw(draw, (x0 + x1) / 2, y, line, title_size, self.style["accent"],
                           bold=True, align="center")
            y += int(title_size * 1.25)
        if subtitle:
            sub = self.type.wrap(subtitle, sub_size, width, 2)
            y += int(sub_size * 0.6)
            for line in sub:
                self.type.draw(draw, (x0 + x1) / 2, y, line, sub_size, (90, 90, 96), align="center")
                y += int(sub_size * 1.35)
        return page

    def photo_page(self, index: int) -> Image.Image:
        page, draw = self._canvas()
        plan = self.pages[index]
        for frame, item in zip(self.geo.frames(plan["layout"]), plan["items"]):
            box = self._place(page, item, frame)
            if self.geo.captions:
                self._caption(draw, item, box)
        self._folio(draw, index + 2)
        return page

    def closing(self) -> Image.Image:
        page, draw = self._canvas()
        g = self.geo
        short = min(g.trim_w, g.trim_h)
        cx = g.bleed + g.trim_w / 2
        cy = g.bleed + g.trim_h / 2
        ornament(draw, self.template, int(cx), int(cy - short * 0.07), max(6, int(short * 0.011)))
        size = int(short * 0.03)
        y = cy + short * 0.02
        for text, colour, bold in ((self.spec.get("title") or "", self.style["accent"], True),
                                   (self.spec.get("subtitle") or "", (90, 90, 96), False),
                                   (self.spec.get("closing") or "", self.style["second"], False)):
            if not text:
                continue
            line = self.type.wrap(text, size, int(g.trim_w - 2 * g.margin), 1, bold=bold)
            if line:
                self.type.draw(draw, cx, y, line[0], size, colour, bold=bold, align="center")
            y += size * 1.6
            size = int(short * 0.022)
        return page

    def render(self) -> Iterable[Image.Image]:
        """Every page in order, one at a time."""
        yield self.cover()
        for index in range(len(self.pages)):
            yield self.photo_page(index)
        yield self.closing()


# ---------------------------------------------------------------------------
# The PDF
# ---------------------------------------------------------------------------

def _pdf_text(text: str) -> bytes:
    """A PDF text string any reader shows in any script: UTF-16 with its mark."""
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">"


class PdfWriter:
    """A PDF of whole-page JPEGs, written one page at a time.

    Each page is a picture the size of the sheet with nothing else on it, so
    the format needed is a small one: an image, a three-operator content
    stream, and a page that points at both. The cross-reference table is
    kept as offsets (a few bytes a page) and written at the end. With bleed,
    each page also says where it is to be cut (TrimBox), which is how a
    print shop's software finds the trim without being told.
    """

    def __init__(self, fh, title: str = "") -> None:  # noqa: ANN001
        self.fh = fh
        self.title = title
        self.offsets: dict[int, int] = {}
        self.kids: list[int] = []
        self.next_id = 4                        # 1 catalog, 2 page tree, 3 info
        fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def _begin(self, number: int) -> None:
        self.offsets[number] = self.fh.tell()
        self.fh.write(b"%d 0 obj\n" % number)

    def _object(self, number: int, body: bytes) -> None:
        self._begin(number)
        self.fh.write(body + b"\nendobj\n")

    def _stream(self, number: int, head: bytes, data: bytes) -> None:
        self._begin(number)
        self.fh.write(b"<< " + head + b" /Length %d >>\nstream\n" % len(data))
        self.fh.write(data)
        self.fh.write(b"\nendstream\nendobj\n")

    def add_page(self, jpeg: bytes, width: int, height: int, bleed: int = 0) -> None:
        image_id, content_id, page_id = self.next_id, self.next_id + 1, self.next_id + 2
        self.next_id += 3
        self._stream(image_id, b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                     b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode"
                     % (width, height), jpeg)
        w_pt, h_pt, b_pt = (v * 72.0 / DPI for v in (width, height, bleed))
        self._stream(content_id, b"", b"q %.3f 0 0 %.3f 0 0 cm /Im0 Do Q" % (w_pt, h_pt))
        boxes = b"/MediaBox [0 0 %.3f %.3f]" % (w_pt, h_pt)
        if bleed:
            boxes += (b" /BleedBox [0 0 %.3f %.3f] /TrimBox [%.3f %.3f %.3f %.3f]"
                      % (w_pt, h_pt, b_pt, b_pt, w_pt - b_pt, h_pt - b_pt))
        self._object(page_id, b"<< /Type /Page /Parent 2 0 R " + boxes
                     + b" /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                     % (image_id, content_id))
        self.kids.append(page_id)

    def close(self) -> None:
        kids = b" ".join(b"%d 0 R" % k for k in self.kids)
        self._object(2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(self.kids))
        self._object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        stamp = time.strftime("D:%Y%m%d%H%M%S")
        self._object(3, b"<< /Title " + _pdf_text(self.title) + b" /Producer (Ninaivu)"
                     b" /CreationDate (" + stamp.encode() + b") >>")
        start = self.fh.tell()
        size = self.next_id
        self.fh.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
        for number in range(1, size):
            self.fh.write(b"%010d 00000 n \n" % self.offsets.get(number, 0))
        self.fh.write(b"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                      % (size, start))


class Cancelled(Exception):
    """The person building the book asked for it to stop."""


def render_book(spec: dict[str, Any], out_path: Path | str, *,
                report: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] | None = None,
                type_: Type | None = None) -> int:
    """Write the book described by *spec* to *out_path*; returns its page count.

    ``report(done, total)`` is told after every page, and ``cancelled()``
    asked before every page: a book stopped halfway leaves no file behind.
    """
    book = Book(spec, type_)
    total = book.page_count
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(out_path, "wb") as fh:
            writer = PdfWriter(fh, spec.get("title") or "")
            for done, page in enumerate(book.render(), start=1):
                if cancelled and cancelled():
                    raise Cancelled()
                buffer = io.BytesIO()
                page.save(buffer, "JPEG", quality=PAGE_QUALITY, subsampling=0,
                          optimize=False, dpi=(DPI, DPI))
                width, height = page.size
                page.close()
                writer.add_page(buffer.getvalue(), width, height, book.geo.bleed)
                del buffer
                if report:
                    report(done, total)
            writer.close()
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    return total


# ---------------------------------------------------------------------------
# Building in the background
# ---------------------------------------------------------------------------

#: Books built at once, across the whole server. One: a page at 300 dpi is
#: tens of megabytes, and on a Raspberry Pi two at a time would be most of the
#: memory the gallery is also serving from.
MAX_BUILDING = 1

_live: dict[int, dict[str, Any]] = {}
_live_lock = threading.Lock()


class Busy(RuntimeError):
    """Another book is being built."""


def building() -> bool:
    with _live_lock:
        return any(job["state"] == "building" for job in _live.values())


def progress(book_id: int) -> dict[str, Any] | None:
    """Pages done and pages in all, while a book is being built here."""
    with _live_lock:
        job = _live.get(int(book_id))
        return None if job is None else {"done": job["done"], "total": job["total"],
                                         "state": job["state"]}


def cancel(book_id: int) -> bool:
    with _live_lock:
        job = _live.get(int(book_id))
        if job is None or job["state"] != "building":
            return False
        job["cancel"].set()
        return True


def start(book_id: int, spec: dict[str, Any], *, db_path: Path | str,
          state_dir: Path | str) -> None:
    """Build book *book_id* on a thread, recording how it ended in the index.

    Busy if a book is already being built. The finished PDF is written under
    a temporary name and renamed into place, so a download can never start
    on half a book.
    """
    from ..storage import books as store, db                 # noqa: PLC0415

    book_id = int(book_id)
    with _live_lock:
        if any(job["state"] == "building" for job in _live.values()):
            raise Busy("A book is already being made. Try again when it is finished.")
        stop = threading.Event()
        _live[book_id] = {"done": 0, "total": 0, "state": "building", "cancel": stop}

    folder = store.books_dir(state_dir)
    name = f"book-{book_id}-{os.urandom(6).hex()}.pdf"
    part = folder / (name + ".part")

    def report(done: int, total: int) -> None:
        with _live_lock:
            _live[book_id].update(done=done, total=total)

    def run() -> None:
        outcome, error, pages = "error", "", 0
        try:
            folder.mkdir(parents=True, exist_ok=True)
            pages = render_book(spec, part, report=report, cancelled=stop.is_set)
            os.replace(part, folder / name)
            outcome = "done"
        except Cancelled:
            outcome = "cancelled"
        except Exception as exc:                               # noqa: BLE001 - recorded, not raised
            log.exception("book %s could not be made", book_id)
            error = str(exc) or exc.__class__.__name__
        finally:
            part.unlink(missing_ok=True)
        try:
            conn = db.connect(db_path)
            if outcome == "done":
                path = folder / name
                if store.get(conn, book_id) is None:
                    # Deleted while it was being made: nothing to keep it for.
                    path.unlink(missing_ok=True)
                else:
                    store.set_state(conn, book_id, "done", file=name,
                                    size_bytes=path.stat().st_size, pages=pages)
            elif store.get(conn, book_id) is not None:
                store.set_state(conn, book_id, outcome, error=error)
        except Exception:                                      # noqa: BLE001
            log.exception("book %s: its state could not be recorded", book_id)
        finally:
            db.close_all()
            with _live_lock:
                job = _live.get(book_id)
                if job is not None:
                    job["state"] = outcome
                # Only the newest few are remembered; the table has the rest.
                for old in [k for k, j in _live.items() if j["state"] != "building"][:-8]:
                    _live.pop(old, None)

    threading.Thread(target=run, name=f"photo-book-{book_id}", daemon=True).start()


def wait(book_id: int, timeout: float = 60.0) -> str:
    """Until a book is no longer being built; for tests and the command line."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = (progress(book_id) or {}).get("state")
        if state != "building":
            return state or ""
        time.sleep(0.05)
    return "building"


def reset() -> None:
    """Forget every build — for tests."""
    with _live_lock:
        _live.clear()
