"""Share without strangers: faces nobody has named are blurred on share links.

A share link is opened by somebody Ninaivu has never met, and a family
photograph carries more people than the family: the waiter, the neighbours'
children at the birthday, the crowd behind the temple car. With
``share_blur_strangers`` on, every photograph a link serves — the full
picture, the browser copy and the small tiles — has those faces blurred,
and the family the household has named stays as it is.

Who counts as named is what the People page says: a face given to a person,
by somebody saying yes or by the matcher being sure. Everybody else is a
stranger, and that includes the faces the face pass never kept: it throws
away the small, soft and half-turned ones because they are no good for
recognising anybody, and those are exactly the people at the back. So the
photograph is looked at again here, at the lower bar the retouching tools
use, and every face found that is not one of the named ones is blurred.

When that cannot be done the photograph goes out blurred all over rather
than as it is: the face model is not downloaded and the face pass has never
looked at it, or the photograph is hidden, which AI does not read
(hidden-not-read-by-ai). Videos are shared as they are; their faces are not
known frame by frame.

The blurred copies are made once and kept with the other browser copies
(media/stills.py), named for the faces they were made from, so naming
somebody later makes the next request build a new copy with them clear.
At most two are made at once (api_share.BLUR_BUILDS); a tile that would
wait longer is sent blurred all over for now, and its real copy is made
behind it, so the first visit to a big link never fills the web threads.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image, ImageDraw, ImageFilter

from . import upright

log = logging.getLogger(__name__)

#: Bumped when the blur changes in a way that makes older copies wrong.
BLUR_VERSION = 1

#: How far past the detector's box the blur reaches, as a share of the face's
#: size. The box ends at the chin and the hairline; ears, hair and a turned
#: cheek are recognisable too.
GROW = 0.35

#: How many blocks across a face is reduced to before it is smoothed. Eight
#: leaves a skin-coloured shape and nothing anybody could recognise.
BLOCKS = 8

#: A located face whose box overlaps a named one this much is that person.
SAME_FACE = 0.4

#: The whole-picture blur, as a share of the longest edge, for a photograph
#: whose faces could not be looked at.
ALL_OVER = 1 / 30


Box = tuple[float, float, float, float]          # x, y, w, h in the photograph's pixels


def stored_faces(conn, asset_id: int) -> list[dict[str, Any]]:
    """The faces the face pass kept, each with whether it is somebody named."""
    rows = conn.execute(
        "SELECT f.bbox, f.source, f.person_id, p.name FROM faces f "
        "LEFT JOIN people_clusters p ON p.id = f.person_id "
        "WHERE f.asset_id=? ORDER BY f.id", (int(asset_id),)).fetchall()
    found = []
    for row in rows:
        try:
            x, y, w, h = (float(v) for v in json.loads(row["bbox"]))
        except (TypeError, ValueError):
            continue
        named = (row["person_id"] is not None
                 and (row["source"] or "") in ("auto", "confirmed")
                 and bool((row["name"] or "").strip()))
        found.append({"box": (x, y, w, h), "named": named})
    return found


def looked_at(conn, asset_id: int) -> bool:
    """Has the face pass ever been over this photograph?

    ``face_version`` is put back to 0 after a turn and when the face model is
    changed, with the faces it found kept (and turned), so stored faces count
    as looked at too.
    """
    row = conn.execute("SELECT face_version FROM assets WHERE id=?",
                       (int(asset_id),)).fetchone()
    if row is not None and int(row[0] or 0) > 0:
        return True
    return conn.execute("SELECT 1 FROM faces WHERE asset_id=? LIMIT 1",
                        (int(asset_id),)).fetchone() is not None


def key(conn, asset: dict[str, Any]) -> str:
    """What a blurred copy of this photograph depends on, short enough for a
    file name: its turn, its faces and who of them is named, and the blur."""
    faces = stored_faces(conn, int(asset["id"]))
    turn = int(asset.get("rotation") or 0) % 360
    hidden = int(asset.get("visibility") or 0) >= 2
    seen = looked_at(conn, int(asset["id"]))
    text = json.dumps([BLUR_VERSION, turn, hidden, seen,
                       [[round(v) for v in f["box"]] + [f["named"]] for f in faces]])
    return hashlib.blake2s(text.encode(), digest_size=6).hexdigest()


def overlap(a: Box, b: Box) -> float:
    """How much two boxes share, over the smaller of them (0 to 1)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    wide = min(ax + aw, bx + bw) - max(ax, bx)
    tall = min(ay + ah, by + bh) - max(ay, by)
    if wide <= 0 or tall <= 0:
        return 0.0
    smaller = min(aw * ah, bw * bh)
    return (wide * tall) / smaller if smaller > 0 else 0.0


def strangers(stored: Iterable[dict[str, Any]], located: Iterable[Box]) -> list[Box]:
    """Every face to blur: the stored ones nobody named, and every face found
    now that is not one of the named ones."""
    stored = list(stored)
    named = [f["box"] for f in stored if f["named"]]
    blur = [f["box"] for f in stored if not f["named"]]
    for box in located:
        if any(overlap(box, mine) >= SAME_FACE for mine in named):
            continue
        if any(overlap(box, theirs) >= SAME_FACE for theirs in blur):
            continue
        blur.append(tuple(float(v) for v in box))
    return blur


def blur_faces(image: Image.Image, boxes: Iterable[Box]) -> Image.Image:
    """The picture with each box blurred past recognising, edges feathered.

    Reduced to a few blocks first and then smoothed: a Gaussian alone, at a
    radius that still looks like a soft photograph, can be sharpened back far
    enough to recognise somebody; blocks cannot.
    """
    picture = image if image.mode == "RGB" else image.convert("RGB")
    width, height = picture.size
    for x, y, w, h in boxes:
        grow = max(w, h) * GROW
        left = max(0, int(x - grow))
        top = max(0, int(y - grow))
        right = min(width, int(x + w + grow) + 1)
        bottom = min(height, int(y + h + grow) + 1)
        if right - left < 2 or bottom - top < 2:
            continue
        region = picture.crop((left, top, right, bottom))
        across = max(1, BLOCKS)
        small = region.resize((across, max(1, round(across * region.height / region.width))),
                              Image.Resampling.BILINEAR)
        blocked = small.resize(region.size, Image.Resampling.NEAREST)
        soft = blocked.filter(ImageFilter.GaussianBlur(max(1.0, region.width / (across * 1.5))))
        mask = Image.new("L", region.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, region.width - 1, region.height - 1), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, min(region.size) / 12)))
        picture.paste(soft, (left, top), mask)
    return picture


def blur_all(image: Image.Image) -> Image.Image:
    """The whole picture too soft to make out anybody, the scene still there."""
    picture = image if image.mode == "RGB" else image.convert("RGB")
    return picture.filter(ImageFilter.GaussianBlur(max(2.0, max(picture.size) * ALL_OVER)))


def opener(conn, asset: dict[str, Any], *, open_oriented: Callable[[Path], Image.Image],
           locate: Callable[[Image.Image], list[Box]] | None):
    """An ``orient`` for :meth:`StillStore.build`: opens the original the way
    the share page shows it (the index's turn baked in) with the strangers
    blurred.

    *locate* finds every face in a picture (the face model's locate), or is
    None when the model is not there.
    """
    asset_id = int(asset["id"])
    turn = int(asset.get("rotation") or 0) % 360
    hidden = int(asset.get("visibility") or 0) >= 2
    stored = stored_faces(conn, asset_id)
    seen = looked_at(conn, asset_id)

    @contextmanager
    def orient(source: Path):
        with open_oriented(source) as image:
            picture = upright.apply(image, turn).convert("RGB")
        if hidden:
            # Never handed to a face model; the faces it had were forgotten.
            yield blur_all(picture)
            return
        located: list[Box] | None = None
        if locate is not None:
            try:
                located = locate(picture)
            except Exception:                             # noqa: BLE001
                log.exception("share blur: could not look for faces in %s", asset_id)
                located = None
        if located is None and not seen:
            yield blur_all(picture)
            return
        yield blur_faces(picture, strangers(stored, located or []))

    return orient


def tile_opener(full: Path, size: int):
    """An ``orient`` that opens an already blurred copy at a tile's size, for
    the small tiles made from it: blurring each size again would look for the
    faces again."""
    @contextmanager
    def orient(_source: Path):
        with Image.open(full) as image:
            image.load()
            tile = image.convert("RGB")
        tile.thumbnail((int(size), int(size)), Image.Resampling.LANCZOS)
        yield tile
    return orient
