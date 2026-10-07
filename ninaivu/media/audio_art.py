"""A picture for a sound file, so it is not a blank square in the gallery.

A photograph brings its own thumbnail and a video has a frame to show. A sound
file had nothing, and in a grid of pictures a square with nothing in it reads
as something that failed to load — or a file that is broken.

So every sound file gets a picture, the best one there is for it:

* **Its own cover.** Songs usually carry the album art inside the file (ID3,
  MP4, FLAC and Ogg all have a place for it), and that is the picture anybody
  would recognise.

* **Otherwise, a tile that says what it is.** A colour that belongs to its
  album, artist or folder, so the tracks of one album sit together as one
  colour; a sign for the kind of recording — a song, a phone call, a voice
  note, or somebody talking at length (an audiobook, a lecture); the shape of
  the sound itself, drawn from the file; and its title.

Nothing here is the application's own words. The title is the file's — its
tag, or its name tidied up — and is drawn only when the bundled font has the
letters for it. The kind is a sign, not a word, so the tile reads the same in
Tamil as in English.

It never fails. A file that cannot be decoded, or has no tags, still gets a
tile; there is simply less on it.
"""

from __future__ import annotations

import base64
import colorsys
import hashlib
import io
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from . import media

log = logging.getLogger("ninaivu.audio_art")

#: The tile is drawn at the largest thumbnail size, and scaled down from there.
SIZE = 640

#: How many bars the waveform has. Enough to show the shape of a song, few
#: enough that each is still a bar at the smallest thumbnail.
BARS = 48

#: Only this much of a long file is read for its waveform. A song fits in it
#: whole; a two-hour audiobook would be a minute of decoding for a picture.
WAVE_SECONDS = 600

#: Longer than this with no artist or album, and it is talk, not music.
SPOKEN_AFTER = 20 * 60

MUSIC, CALL, VOICE, SPOKEN = "music", "call", "voice", "spoken"

_CALL = re.compile(r"call[\s_-]*rec|recorded[\s_-]*call|(?:^|\D)\+?\d{10,13}(?:\D|$)")
_CALL_FOLDER = re.compile(r"\bcalls?\b|call[\s_-]*rec")
_VOICE = re.compile(r"^(?:voice|rec|recording|new recording|memo|ptt|aud|audio)(?:[\s_-]|\d|$)"
                    r"|voice[\s_-]*(?:note|memo|message)|whatsapp[\s_-]*(?:audio|voice)|recorder")
_SPOKEN = re.compile(r"chapter|(?:^|[^a-z])ch\.?[\s_-]*\d+|\bpart[\s_.-]?\d+|audio[\s_-]?book|lecture"
                     r"|podcast|episode|\bep[\s_.-]?\d+|sermon|discourse|lesson|speech|talk"
                     r"|\(\d+\s+of\s+\d+\)")
_VOICE_EXTENSIONS = {".amr", ".3ga", ".awb", ".opus"}


def classify(rel_path: str, *, duration: float | None = None,
             artist: str | None = None, album: str | None = None) -> str:
    """Which kind of recording this is, from its name, folder and length."""
    path = Path(rel_path.replace("\\", "/"))
    name = path.stem.lower()
    folder = str(path.parent).lower()
    if _CALL.search(name) or _CALL_FOLDER.search(folder):
        return CALL
    if (_VOICE.search(name) or path.suffix.lower() in _VOICE_EXTENSIONS
            or any(w in folder for w in ("voice", "recorder", "recordings"))):
        return VOICE
    if _SPOKEN.search(name) or any(w in folder for w in ("audiobook", "podcast", "lecture")):
        return SPOKEN
    if duration and duration >= SPOKEN_AFTER and not (artist or album):
        return SPOKEN
    return MUSIC


# --- what the file says about itself ----------------------------------------

def read_tags(path: Path) -> dict[str, Any]:
    """Title, artist, album and cover art, from one read of the file."""
    found: dict[str, Any] = {}
    try:
        import mutagen                                     # noqa: PLC0415

        audio = mutagen.File(str(path))
    except Exception as exc:                               # noqa: BLE001
        log.debug("%s: no tags: %s", path, exc)
        return found
    if audio is None:
        return found
    tags = getattr(audio, "tags", None)

    def first(*keys: str) -> str | None:
        for key in keys:
            try:
                value = tags.get(key) if tags is not None else None
            except (KeyError, ValueError, TypeError):
                value = None
            if value:
                text = str(value[0] if isinstance(value, list) else value).strip()
                if text:
                    return text
        return None

    found["title"] = first("TIT2", "title", "\xa9nam", "TITLE")
    found["artist"] = first("TPE1", "artist", "\xa9ART", "ARTIST", "TPE2", "aART")
    found["album"] = first("TALB", "album", "\xa9alb", "ALBUM")
    found["cover"] = _cover_bytes(audio, tags)
    return found


def _cover_bytes(audio: Any, tags: Any) -> bytes | None:
    try:
        if tags is not None and hasattr(tags, "getall"):           # ID3
            pictures = tags.getall("APIC")
            if pictures:
                front = [p for p in pictures if getattr(p, "type", None) == 3]
                return bytes((front or pictures)[0].data)
        if tags is not None and "covr" in tags:                     # MP4
            return bytes(tags["covr"][0])
        pictures = getattr(audio, "pictures", None)                 # FLAC
        if pictures:
            return bytes(pictures[0].data)
        if tags is not None and "metadata_block_picture" in tags:   # Ogg
            from mutagen.flac import Picture                       # noqa: PLC0415

            return bytes(Picture(base64.b64decode(tags["metadata_block_picture"][0])).data)
    except Exception as exc:                                        # noqa: BLE001
        log.debug("no usable cover: %s", exc)
    return None


def cover_image(data: bytes | None) -> Image.Image | None:
    """The embedded cover as a picture, or None when it is not one."""
    if not data:
        return None
    from .safe_image import UNTRUSTED_MAX_PIXELS          # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(data)) as picture:
            # The size is checked before a pixel is decoded. A sound file can
            # arrive in an upload, and the upload's own size check only looks
            # at pictures: a small MP3 whose cover declared 20000 x 20000 cost
            # over a gigabyte to open. No album cover comes near this limit.
            width, height = picture.size
            if width * height > UNTRUSTED_MAX_PIXELS:
                log.debug("cover art declares %d x %d pixels; not opened", width, height)
                return None
            # The tile is SIZE pixels, so a JPEG cover is decoded no larger.
            picture.draft("RGB", (SIZE, SIZE))
            picture.load()
            if min(picture.size) < 32:
                return None
            return ImageOps.exif_transpose(picture).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        log.debug("cover art could not be read: %s", exc)
        return None


def waveform(path: Path, bars: int = BARS) -> list[float] | None:
    """The loudness of the sound in ``bars`` slices, each 0–1. None without ffmpeg."""
    if not media.FFMPEG:
        return None
    try:
        import numpy as np                                 # noqa: PLC0415

        proc = subprocess.run(
            [media.FFMPEG, "-v", "quiet", "-nostdin", "-t", str(WAVE_SECONDS),
             *media.LOCAL_ONLY, "-i", str(path), "-vn", "-ac", "1", "-ar", "2000",
             "-f", "s16le", "-"],
            capture_output=True, timeout=90, check=False)
        samples = np.frombuffer(proc.stdout, dtype="<i2").astype("float32")
    except (OSError, subprocess.SubprocessError, ImportError, ValueError) as exc:
        log.debug("%s: no waveform: %s", path, exc)
        return None
    if samples.size < bars * 4:
        return None
    chunks = np.array_split(np.abs(samples), bars)
    levels = np.array([float(np.sqrt(np.mean(c * c))) for c in chunks])
    top = float(np.percentile(levels, 95)) or float(levels.max())
    if top <= 0:
        return [0.0] * bars
    # The square root is closer to how loud it sounds than the raw level is,
    # and keeps quiet passages visible beside loud ones.
    return [min(1.0, (float(v) / top) ** 0.5) for v in levels]


# --- the title --------------------------------------------------------------

_TRACK = re.compile(r"^\s*(?:\d{1,3}\s*[-._)]\s*|\d{1,3}\s+(?=\D))")
_STAMP = re.compile(r"[+-]?\d{6,}|\d{4}-\d{2}-\d{2}|\d{1,2}[.:]\d{2}[.:]\d{2}")


def title_for(rel_path: str, kind: str, tag_title: str | None) -> str:
    """What to call it: its tag, or its name tidied up."""
    if tag_title:
        return " ".join(tag_title.split())
    name = Path(rel_path.replace("\\", "/")).name
    name = re.sub(r"(\.drm)?\.[A-Za-z0-9]{2,4}$", "", name)
    if kind == CALL:
        name = re.sub(r"call[\s_-]*recording", " ", name, flags=re.I)
        name = _STAMP.sub(" ", name)
        name = re.sub(r"(?:^|[\s_-])[OI](?=$|[\s_.-])", " ", name)   # _O / _I: out, in
    name = re.sub(r"^copy\s*\(?\d*\)?(?:\s+of)?[\s_]*", "", name, flags=re.I)
    name = re.sub(r"^[^\w(]+", "", name)
    name = _TRACK.sub("", name)
    name = re.sub(r"^0\d(?=[A-Z])", "", name)          # "05Irupathu", a track number
    name = re.sub(r"[_]+", " ", name)
    name = re.sub(r"\s*-\s*$", "", name)
    name = " ".join(name.split()).strip(" .-_")
    if re.fullmatch(r"[0-9a-f]{16,}", name.lower()):    # a hash is not a name
        return ""
    if name.isupper() and len(name) > 3:
        name = name.title()
    return name


def _drawable(text: str) -> bool:
    """Whether the bundled font has the letters: Latin-1 and no further. Not
    Tamil, and not even a dash or curly quotes — and a row of empty boxes is
    worse than no title at all."""
    return bool(text) and all(ord(ch) < 0x100 for ch in text)


# --- the tile ---------------------------------------------------------------

#: The colour family each kind is drawn in, and how far a tile may wander from
#: it. Music takes any colour, so an album is its own.
_HUES = {MUSIC: (None, 180), CALL: (152, 26), VOICE: (26, 18), SPOKEN: (250, 28)}


def colours(kind: str, seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """A light and a dark shade, the same every time for the same seed."""
    digest = int(hashlib.sha1(seed.lower().encode("utf-8"),
                              usedforsecurity=False).hexdigest()[:8], 16)
    base, spread = _HUES.get(kind, _HUES[MUSIC])
    if base is None:
        hue = digest % 360
    else:
        hue = (base + digest % (2 * spread + 1) - spread) % 360

    def rgb(lightness: float, saturation: float) -> tuple[int, int, int]:
        r, g, b = colorsys.hls_to_rgb(hue / 360, lightness, saturation)
        return int(r * 255), int(g * 255), int(b * 255)

    return rgb(0.46, 0.52), rgb(0.24, 0.55)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:            # Pillow before 10.1 has only the tiny bitmap
        return None


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, lines: int) -> list[str]:
    words, out, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
            continue
        if line:
            out.append(line)
        line = word
        if len(out) == lines:
            break
    if line and len(out) < lines:
        out.append(line)
    cut = len(out) == lines and " ".join(out) != " ".join(words)
    for index, text in enumerate(out):
        if cut and index == len(out) - 1 or draw.textlength(text, font=font) > width:
            while text and draw.textlength(text + "...", font=font) > width:
                text = text[:-1]
            out[index] = text.rstrip() + "..."
    return [draw_line for draw_line in out if draw_line]


def _sign(draw: ImageDraw.ImageDraw, kind: str, cx: float, cy: float, s: float,
          ink: tuple[int, int, int, int]) -> None:
    """The kind of recording, as a sign ``s`` pixels across."""
    w = max(3, int(s * 0.075))
    u = s / 24.0
    x0, y0 = cx - s / 2, cy - s / 2

    def p(x: float, y: float) -> tuple[float, float]:
        return x0 + x * u, y0 + y * u

    def dot(x: float, y: float, r: float) -> None:
        (a, b), (c, d) = p(x - r, y - r), p(x + r, y + r)
        draw.ellipse([a, b, c, d], fill=ink)

    if kind == CALL:                  # a phone
        (a, b), (c, d) = p(6.5, 2), p(17.5, 22)
        draw.rounded_rectangle([a, b, c, d], radius=int(3 * u), outline=ink, width=w)
        draw.line([p(10, 5), p(14, 5)], fill=ink, width=w)
        dot(12, 18.6, 1.2)
    elif kind == VOICE:               # a microphone
        (a, b), (c, d) = p(8.5, 2), p(15.5, 14.5)
        draw.rounded_rectangle([a, b, c, d], radius=int(3.5 * u), outline=ink, width=w)
        (a, b), (c, d) = p(5, 7), p(19, 18)
        draw.arc([a, b, c, d], 0, 180, fill=ink, width=w)
        draw.line([p(12, 18), p(12, 22)], fill=ink, width=w)
        draw.line([p(8.5, 22), p(15.5, 22)], fill=ink, width=w)
    elif kind == SPOKEN:              # an open book
        draw.line([p(2, 5), p(9, 4), p(12, 6), p(12, 20), p(9, 18.5), p(2, 19.5), p(2, 5)],
                  fill=ink, width=w, joint="curve")
        draw.line([p(22, 5), p(15, 4), p(12, 6)], fill=ink, width=w, joint="curve")
        draw.line([p(12, 20), p(15, 18.5), p(22, 19.5), p(22, 5)], fill=ink, width=w,
                  joint="curve")
    else:                             # a note — the same one the grid draws
        draw.line([p(9, 18), p(9, 6), p(20, 4), p(20, 16)], fill=ink, width=w, joint="curve")
        dot(6.5, 18, 2.9)
        dot(17.5, 16, 2.9)


def tile(kind: str, *, title: str = "", subtitle: str = "", seed: str = "",
         levels: Sequence[float] | None = None) -> Image.Image:
    """The drawn picture for a sound file with no cover of its own."""
    light, dark = colours(kind, seed or title or kind)
    down = Image.linear_gradient("L").resize((SIZE, SIZE))
    ramp = ImageChops.add(down, down.transpose(Image.Transpose.ROTATE_90).transpose(
        Image.Transpose.FLIP_LEFT_RIGHT), scale=2.0)
    image = ImageOps.colorize(ramp, black=light, white=dark).convert("RGBA")
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    has_title = _drawable(title)
    _sign(draw, kind, SIZE / 2, SIZE * (0.30 if has_title else 0.36), SIZE * 0.30,
          (255, 255, 255, 235))

    if has_title:
        font = _font(42)
        if font is not None:
            y = SIZE * 0.52
            for line in _wrap(draw, title, font, SIZE - 96, 2):
                draw.text((SIZE / 2, y), line, font=font, fill=(255, 255, 255, 245),
                          anchor="ma")
                y += 50
            small = _font(28)
            if small is not None and _drawable(subtitle):
                for line in _wrap(draw, subtitle, small, SIZE - 120, 1):
                    draw.text((SIZE / 2, y + 6), line, font=small,
                              fill=(255, 255, 255, 170), anchor="ma")

    if levels:
        margin, gap = 56, 4
        span = SIZE - 2 * margin
        bar = span / len(levels)
        middle, reach = SIZE * 0.85, SIZE * 0.085
        for index, level in enumerate(levels):
            height = max(3.0, float(level) * reach)
            left = margin + index * bar + gap / 2
            draw.rounded_rectangle(
                [left, middle - height, left + bar - gap, middle + height],
                radius=int(max(1, (bar - gap) / 2)), fill=(255, 255, 255, 150))

    return Image.alpha_composite(image, layer).convert("RGB")


def picture_for(path: str | Path, rel_path: str, *,
                duration: float | None = None) -> Image.Image:
    """The best picture there is for this sound file. Never raises."""
    path = Path(path)
    try:
        tags = read_tags(path)
        cover = cover_image(tags.get("cover"))
        if cover is not None:
            return cover
        kind = classify(rel_path, duration=duration,
                        artist=tags.get("artist"), album=tags.get("album"))
        title = title_for(rel_path, kind, tags.get("title"))
        if not title:
            title = tags.get("album") or tags.get("artist") or ""
        folder = Path(rel_path.replace("\\", "/")).parent.name
        # One colour per album, or per artist, or per person on the phone, or
        # per folder — whichever the file can say it belongs to.
        seed = tags.get("album") or tags.get("artist") or (title if kind == CALL else folder)
        subtitle = next((t for t in (tags.get("artist"), tags.get("album"))
                         if t and t != title), "")
        return tile(kind, title=title, subtitle=subtitle, seed=seed or title,
                    levels=waveform(path))
    except Exception as exc:                               # noqa: BLE001
        log.debug("%s: plain tile: %s", path, exc)
        return tile(MUSIC, seed=rel_path)
