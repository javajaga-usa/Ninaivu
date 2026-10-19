"""What the job engine can work out for a photograph, and how it is kept.

A derivation is one thing worked out from a file: its words, its faces, and
in later releases its caption or a voice story's transcript. Each declares:

* its **version** — the version of the reader or model that makes it. What
  the index holds is stamped with it (``ocr_version``, ``face_version``), so
  a better model makes only its own results owed again;
* what is **owed** — the photographs whose stamp is older, which is what
  fills the task list (the same question the 1.x passes ask, so turning the
  engine on analyses nothing again);
* the **job** a worker process does for one photograph (work.py), given
  plain values only;
* how its answer is **stored**, by the engine's one writer, in a group;
* and what is stored when a photograph **fails** for good.

The order between derivations is in what each reads: text reads the
thumbnail, so it is only owed once a thumbnail exists; faces read the
original. A new derivation is one more entry in :data:`DERIVATIONS`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..storage import db
from . import work

log = logging.getLogger(__name__)


@dataclass
class Context:
    """What the derivations of one run need from the server, worked out once."""

    cfg: Any
    root: str
    #: Threads each worker's text reader may use.
    ocr_threads: int = 1
    #: Where the face models are (the scan's FaceEngine's state folder).
    faces_state: str = ""

    @property
    def preview_ext(self) -> str:
        return "webp" if str(self.cfg.thumb_format).upper() == "WEBP" else "jpg"

    def preview(self, thumb: str) -> str:
        return str(Path(self.cfg.thumbs_dir) / f"{thumb}_{max(self.cfg.thumb_sizes)}.{self.preview_ext}")

    @property
    def crops_dir(self) -> Path:
        return Path(self.cfg.state_dir) / "faces"


def _as_is(ctx: Context, conn, item: int, result: Any) -> Any:  # noqa: ANN001
    return result


@dataclass(frozen=True)
class Derivation:
    name: str
    #: Higher is handed out first when several are owed.
    priority: int
    version: Callable[[], int]
    #: ``(conn, root, version) -> [(item, rank)]``: what is owed, lower rank first.
    owed: Callable[[Any, str, int], list[tuple[int, float]]]
    #: ``(ctx, conn, items) -> {item: (fn, args)}``: the jobs for *items*
    #: still owed now. An item left out is no longer owed and is settled.
    jobs: Callable[[Context, Any, list[int]], dict[int, tuple[Callable[..., Any], tuple]]]
    #: ``(ctx, conn, item, result) -> bool``: write one answer (inside a
    #: write group); True when it found something.
    store: Callable[[Context, Any, int, Any], bool]
    #: ``(ctx, conn, item) -> None``: what is written for a photograph that
    #: failed :data:`tasks.MAX_ATTEMPTS` times.
    give_up: Callable[[Context, Any, int], None]
    #: Whether a file that is not there settles the task (and is stored as
    #: such) or leaves it owed for when the drive is back.
    missing_settles: bool = True
    #: ``(ctx, conn, item, result) -> result``: run before :attr:`store`,
    #: outside the write group: files to write beside the answer.
    prepare: Callable[[Context, Any, int, Any], Any] = _as_is


# -- reading text ----------------------------------------------------------

def _text_version() -> int:
    from ..media import ocr                               # noqa: PLC0415
    return int(ocr.OCR_VERSION)


def _text_owed(conn, root: str, version: int) -> list[tuple[int, float]]:  # noqa: ANN001
    return [(int(r[0]), float(r[0])) for r in conn.execute(
        "SELECT id FROM assets WHERE root=? AND kind='picture' AND trashed=0 "
        f"AND thumb IS NOT NULL AND ocr_version < ? AND {db.AI_MAY_READ} ORDER BY id",
        (root, version))]


def _rows(conn, items: list[int], columns: str, where: str, params: tuple = ()):  # noqa: ANN001, ANN202
    found = {}
    for start in range(0, len(items), 500):
        chunk = items[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT id, {columns} FROM assets WHERE id IN ({marks}) AND {where}",
                (*chunk, *params)):
            found[int(row[0])] = row
    return found


def _text_jobs(ctx: Context, conn, items: list[int]):  # noqa: ANN001, ANN202
    cfg = ctx.cfg
    rows = _rows(conn, items, "thumb",
                 f"trashed=0 AND thumb IS NOT NULL AND ocr_version < ? AND {db.AI_MAY_READ}",
                 (_text_version(),))
    return {item: (work.read_text, (ctx.preview(row["thumb"]), float(cfg.ocr_min_score),
                                    int(cfg.ocr_max_chars), int(ctx.ocr_threads)))
            for item, row in rows.items()}


def _text_store(ctx: Context, conn, item: int, text: Any) -> bool:  # noqa: ANN001
    if text == work.MISSING:
        # The preview is not there: stamped, as the 1.x pass stamps it, so a
        # photograph whose thumbnail went is not read again on every scan.
        text = ""
    db.store_ai_fields(conn, item, ocr_text=text or None, ocr_version=_text_version())
    return bool(text)


def _text_give_up(ctx: Context, conn, item: int) -> None:  # noqa: ANN001
    # As the 1.x pass: a photograph the reader failed on is not stamped, so
    # a newer reader tries it. The failed task keeps it from being handed
    # out again at this version.
    return None


# -- finding faces ---------------------------------------------------------

def _faces_version() -> int:
    from ..media import faces                             # noqa: PLC0415
    return int(faces.FACE_VERSION)


def _faces_owed(conn, root: str, version: int) -> list[tuple[int, float]]:  # noqa: ANN001
    # Newest first, as the 1.x pass goes: this year's photographs are the
    # ones the family is looking at.
    return [(int(r[0]), -float(r[1] or 0)) for r in conn.execute(
        "SELECT id, COALESCE(captured_at, mtime) FROM assets "
        "WHERE root=? AND trashed=0 AND kind='picture' AND face_version < ? "
        f"AND {db.AI_MAY_READ}", (root, version))]


def _faces_jobs(ctx: Context, conn, items: list[int]):  # noqa: ANN001, ANN202
    rows = _rows(conn, items, "rel_path, rotation",
                 f"trashed=0 AND kind='picture' AND face_version < ? AND {db.AI_MAY_READ}",
                 (_faces_version(),))
    return {item: (work.find_faces, (ctx.faces_state, str(Path(ctx.root) / row["rel_path"]),
                                     int(row["rotation"] or 0)))
            for item, row in rows.items()}


def _faces_prepare(ctx: Context, conn, item: int, detected: Any) -> Any:  # noqa: ANN001
    """The rows to store, with the crops written: outside the write group,
    so a family member's write never waits on the crops of a group shot."""
    from ..media import faces                             # noqa: PLC0415
    if detected is None:
        return None
    if not db.ai_may_read(conn, item):
        return []               # hidden while it was looked at: no crops of it
    rows: list[dict[str, Any]] = []
    for index, face in enumerate(detected):
        record = face.as_row()
        record["embedding"] = faces.pack(face.embedding)
        name = f"{item}_{index}.jpg"
        if faces.write_crop(face.crop, ctx.crops_dir / name):
            record["thumb"] = name
        rows.append(record)
    return rows


def _faces_store(ctx: Context, conn, item: int, rows: Any) -> bool:  # noqa: ANN001
    from ..media import faces                             # noqa: PLC0415
    if rows is None:
        # Unreadable, or not an image any more: done anyway, as the 1.x pass
        # marks it, or every pass retries the same broken file for ever.
        db.mark_faces_scanned(conn, item, _faces_version())
        return False
    return db.replace_asset_faces(conn, item, rows, faces.MODEL_ID) > 0


def _faces_give_up(ctx: Context, conn, item: int) -> None:  # noqa: ANN001
    # Stamped, as the 1.x pass stamps a file it cannot read: a file that
    # crashed the detector twice would otherwise be handed out on every scan.
    db.mark_faces_scanned(conn, item, _faces_version())


DERIVATIONS: dict[str, Derivation] = {
    "text": Derivation("text", 20, _text_version, _text_owed, _text_jobs,
                       _text_store, _text_give_up),
    "faces": Derivation("faces", 10, _faces_version, _faces_owed, _faces_jobs,
                        _faces_store, _faces_give_up, missing_settles=False,
                        prepare=_faces_prepare),
}
