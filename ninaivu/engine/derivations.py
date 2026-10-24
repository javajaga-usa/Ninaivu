"""What the job engine can work out for a photograph, and how it is kept.

A derivation is one thing worked out from a file: its words, its faces,
a sound file's picture, a video's description from its moments, a
file's fingerprint; in later releases a caption or a voice story's
transcript. Each declares:

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
    #: The moments of a clip to read, as fractions of its length (the
    #: scan's ``KEYFRAME_POINTS``, as many as ``video_keyframes`` asks for).
    keyframe_points: tuple[float, ...] = ()
    #: Whether a clip is also read for the explicit check
    #: (``scanner.explicit_points``).
    keyframe_explicit: bool = False
    #: ``(jpeg frames) -> fields``: the image model's description of a clip
    #: from its moments, worked out in the server, where the model is.
    describe_clip: Callable[[list[bytes]], dict[str, Any]] | None = None
    #: Files fingerprinted at once: the drive is the limit, not the cores.
    fingerprint_readers: int = 2

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


def _yes() -> bool:
    return True


def _readers_room() -> int:
    from ..media import ocr                               # noqa: PLC0415
    return int(ocr.readers_room())


def _with_ffmpeg() -> bool:
    """Clips are read by ffmpeg, a process of its own; without it OpenCV
    decodes them, which in 1.x could take the server down."""
    from ..media import media                             # noqa: PLC0415
    return bool(media.FFMPEG)


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
    #: ``(ctx) -> int``: at most this many in flight at once, for work the
    #: cores are not the limit of (a drive read end to end). None: as many
    #: as there are workers.
    at_once: Callable[[Context], int] | None = None
    #: Whether :attr:`owed` is also given the settings, as a fourth argument
    #: (what is owed is on disk rather than in the index).
    owed_with_cfg: bool = False
    #: ``() -> bool``: whether a run of a few of these may work on threads
    #: of the server (pool.InProcessPool) rather than start processes. Only
    #: work that decodes no more in the server than the indexer already does
    #: there: a decoder that crashes in a worker takes only the worker down.
    #: Off unless said.
    in_process: Callable[[], bool] = lambda: False
    #: ``() -> int``: at most this many of the server's threads at once,
    #: for work that holds a lot each (a text reader, 400 to 600 MB).
    threads_room: Callable[[], int] | None = None


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
                                     int(row["rotation"] or 0), _face_model(ctx)))
            for item, row in rows.items()}


def _face_model(ctx: Context) -> str:
    return str(getattr(ctx.cfg, "face_model", "") or "sface")


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
    return db.replace_asset_faces(conn, item, rows, faces.model_id_for(_face_model(ctx))) > 0


def _faces_give_up(ctx: Context, conn, item: int) -> None:  # noqa: ANN001
    # Stamped, as the 1.x pass stamps a file it cannot read: a file that
    # crashed the detector twice would otherwise be handed out on every scan.
    db.mark_faces_scanned(conn, item, _faces_version())


# -- pictures for sound files --------------------------------------------

def _covers_version() -> int:
    # Whether one is done is whether its picture is on disk; nothing in the
    # index is stamped, so the task list's version never moves.
    return 1


def _cover_base(root: str, row) -> str:  # noqa: ANN001
    from ..media import media                             # noqa: PLC0415
    return row["thumb"] or media.thumb_base(root, row["rel_path"])


def _covers_owed(conn, root: str, version: int, cfg: Any) -> list[tuple[int, float]]:  # noqa: ANN001
    from ..media import media                             # noqa: PLC0415
    size, fmt = max(cfg.thumb_sizes), cfg.thumb_format
    thumbs = Path(cfg.thumbs_dir)
    return [(int(row["id"]), float(row["id"])) for row in conn.execute(
        "SELECT id, rel_path, thumb FROM assets "
        "WHERE root=? AND kind='audio' AND trashed=0 ORDER BY id", (root,))
        if not (thumbs / media.thumb_file(_cover_base(root, row), size, fmt)).is_file()]


def _covers_jobs(ctx: Context, conn, items: list[int]):  # noqa: ANN001, ANN202
    cfg = ctx.cfg
    rows = _rows(conn, items, "rel_path, thumb, duration", "trashed=0 AND kind='audio'")
    return {item: (work.draw_cover, (str(Path(ctx.root) / row["rel_path"]), row["rel_path"],
                                     row["duration"], str(cfg.thumbs_dir),
                                     _cover_base(ctx.root, row), list(cfg.thumb_sizes),
                                     cfg.thumb_format, int(cfg.thumb_quality)))
            for item, row in rows.items()}


def _covers_store(ctx: Context, conn, item: int, drawn: Any) -> bool:  # noqa: ANN001
    import time                                           # noqa: PLC0415
    # indexed_at is the thumbnail's version in its URL: moved, so no
    # browser keeps the old blank (as Scanner._draw_audio_art).
    db.update_asset(conn, item, thumb=drawn["thumb"], blurhash=drawn["blurhash"],
                    color=drawn["color"], indexed_at=time.time())
    return True


def _nothing(ctx: Context, conn, item: int) -> None:  # noqa: ANN001
    # Nothing stamped: what is owed is worked out afresh at the next scan,
    # and the failed task keeps it from being handed out again meanwhile.
    return None


# -- describing videos by their moments --------------------------------------

def _keyframes_version() -> int:
    from ..media import scanner                           # noqa: PLC0415
    return int(scanner.KEYFRAME_VERSION)


#: The clips owed a description, as SQL on ``assets`` with the version as
#: the parameter. The clip half of a live photograph is not described (see
#: Scanner._tag_video_keyframes).
_CLIPS = ("trashed=0 AND kind='video' AND live_clip=0 AND keyframe_version < ? "
          f"AND {db.AI_MAY_READ}")


def _keyframes_owed(conn, root: str, version: int) -> list[tuple[int, float]]:  # noqa: ANN001
    return [(int(r[0]), float(r[0])) for r in conn.execute(
        f"SELECT id FROM assets WHERE root=? AND {_CLIPS} ORDER BY id", (root, version))]


def _keyframes_jobs(ctx: Context, conn, items: list[int]):  # noqa: ANN001, ANN202
    rows = _rows(conn, items, "rel_path, duration", _CLIPS, (_keyframes_version(),))
    return {item: (work.read_keyframes, (ctx.root, row["rel_path"], row["duration"],
                                         tuple(ctx.keyframe_points),
                                         bool(ctx.keyframe_explicit)))
            for item, row in rows.items()}


def _keyframes_prepare(ctx: Context, conn, item: int, read: Any) -> Any:  # noqa: ANN001
    """The model's description of the clip, outside the write group: the
    model is in this process, and a family member's write never waits for
    it. A clip the model cannot describe is stamped done, as the 1.x pass
    does, rather than handed out again on every scan."""
    frames = read.get("frames")
    described = None
    if frames:
        try:
            described = ctx.describe_clip(frames) if ctx.describe_clip else None
        except Exception as exc:  # noqa: BLE001 - one bad clip, not a scan
            log.debug("keyframes for item %d: %s", item, exc)
    return {"fields": read.get("fields") or {}, "described": described,
            "later": bool(read.get("later")) and not frames}


def _keyframes_store(ctx: Context, conn, item: int, read: Any) -> bool:  # noqa: ANN001
    version = _keyframes_version()
    if read["fields"]:
        db.update_asset(conn, item, **read["fields"])
    if read["later"]:
        return False                     # tried again at the next scan
    described = read["described"]
    if not described:
        db.update_asset(conn, item, keyframe_version=version)
        return False
    fields = dict(described)
    fields["keyframe_version"] = version
    embedding = fields.pop("embedding", None)
    model_id = fields.pop("model_id", None)
    # As in Scanner._tag_batch: an admin's own tags, caption and flag are kept.
    db.store_ai_fields(conn, item, **fields)
    if embedding is not None and model_id:
        db.store_embedding(conn, item, model_id, len(embedding) // 4, embedding)
    return True


def _keyframes_give_up(ctx: Context, conn, item: int) -> None:  # noqa: ANN001
    # A clip that brought a worker down twice: stamped, as the 1.x pass
    # stamps a clip it could not describe.
    db.update_asset(conn, item, keyframe_version=_keyframes_version())


# -- fingerprints -------------------------------------------------------------

def _fingerprints_version() -> int:
    from ..storage import fingerprints                    # noqa: PLC0415
    # BLAKE2b ones are made again once BLAKE3 is there (fingerprints.ALGO).
    return 2 if fingerprints.ALGO == "blake3" else 1


def _fingerprints_owed(conn, root: str, version: int) -> list[tuple[int, float]]:  # noqa: ANN001
    from ..storage import fingerprints                    # noqa: PLC0415
    return [(int(r["id"]), float(r["id"])) for r in fingerprints.owed_rows(conn, root)]


def _fingerprints_jobs(ctx: Context, conn, items: list[int]):  # noqa: ANN001, ANN202
    from ..storage import fingerprints                    # noqa: PLC0415
    rows = _rows(conn, items, "rel_path", f"trashed=0 AND NOT {fingerprints.UP_TO_DATE}",
                 (fingerprints.ALGO,))
    return {item: (work.fingerprint, (str(Path(ctx.root) / row["rel_path"]),))
            for item, row in rows.items()}


def _fingerprints_store(ctx: Context, conn, item: int, taken: Any) -> bool:  # noqa: ANN001
    from ..storage import fingerprints                    # noqa: PLC0415
    if not isinstance(taken, (tuple, list)):
        # Gone (the scan removes its row), changed while read (the scan
        # indexes it again) or unreadable: nothing kept, owed next time.
        return False
    digest, size, mtime = taken
    row = conn.execute("SELECT size, mtime FROM assets WHERE id=?", (item,)).fetchone()
    if row is None or size != row[0] or abs(float(mtime) - float(row[1] or 0)) > 1e-6:
        return False                     # the next scan reads it again
    # Stored at the row's own values, as fingerprints.fill stores them.
    fingerprints.store(conn, [(item, digest, int(row[0]), float(row[1]))])
    return True


DERIVATIONS: dict[str, Derivation] = {
    # From the thumbnails Ninaivu wrote itself.
    "text": Derivation("text", 20, _text_version, _text_owed, _text_jobs,
                       _text_store, _text_give_up, in_process=_yes,
                       threads_room=_readers_room),
    "faces": Derivation("faces", 10, _faces_version, _faces_owed, _faces_jobs,
                        _faces_store, _faces_give_up, missing_settles=False,
                        prepare=_faces_prepare),
    # Sound files' pictures are thumbnails, wanted while the gallery is
    # open; a file on a drive that is not plugged in waits for it.
    "covers": Derivation("covers", 30, _covers_version, _covers_owed, _covers_jobs,
                         _covers_store, _nothing, missing_settles=False,
                         owed_with_cfg=True, in_process=_yes),
    "keyframes": Derivation("keyframes", 15, _keyframes_version, _keyframes_owed,
                            _keyframes_jobs, _keyframes_store, _keyframes_give_up,
                            prepare=_keyframes_prepare, in_process=_with_ffmpeg),
    # Last, and two files at a time: it reads every byte once, and the
    # drive is the limit, not the cores (fingerprints.fill).
    "fingerprints": Derivation("fingerprints", 0, _fingerprints_version,
                               _fingerprints_owed, _fingerprints_jobs,
                               _fingerprints_store, _nothing,
                               at_once=lambda ctx: ctx.fingerprint_readers, in_process=_yes),
}
