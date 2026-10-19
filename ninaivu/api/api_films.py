"""Memory films in the family app: pick, make, play, download (media/films.py).

A trial of Ninaivu 2.0, off unless ``memory_films`` is switched on (Settings
→ Advanced); off, every route here answers 404 as if it were not there.

The same rules as photo books (api/api_books.py), whose sources and pickers
these are: every photograph and clip is found through ``db.query_assets``
with this viewer's own limits, a voice story is only told over a photograph
this viewer may open (a story has no visibility of its own, storage/stories.py),
a song is only one this viewer could have found in the gallery, and the
background process is handed a fixed list of files and never asks the index
what else there is. Guests make no films.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from flask import abort, jsonify, request, send_file

from ..media import books as book_maker, films as maker
from ..server.auth import current_user, require_family
from ..storage import db, films as store, stories as story_store
from ..words import Said
from .api import _cfg, _conn, _roots, _safe_under, _thumb_version, _viewer, _visible_ids, bp
from .api_books import (MAX_CANDIDATES, _CHUNK, _faces, _human_caption, _in_chunks, _screens,
                        _signals, _source, _text)

#: The most songs offered to choose from.
MAX_SONGS = 300


def _switched_on() -> None:
    if not getattr(_cfg(), "memory_films", False):
        abort(404)


def _query(filters: dict[str, Any], ids: list[int] | None = None, *,
           columns: tuple[str, ...] | None = None, limit: int = 100000,
           kinds: tuple[str, ...] = ("picture", "video")) -> list[dict[str, Any]]:
    """Photographs and videos of the source, as this viewer may see them, oldest first."""
    wanted = dict(filters)
    if ids is not None:
        allowed = wanted.pop("ids", None)
        if allowed is not None:
            allowed = set(allowed)
            ids = [i for i in ids if i in allowed]
        wanted["ids"] = ids
        limit = len(ids) or 1
    rows, _ = db.query_assets(_conn(), _roots(), kinds=list(kinds), sort="date_asc",
                              limit=limit, offset=0, columns=columns, with_total=False,
                              **_viewer(), **wanted)
    return rows


def _rows(filters: dict[str, Any], ids: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for start in range(0, len(ids), _CHUNK):
        out += _query(filters, ids[start:start + _CHUNK])
    out.sort(key=lambda r: (book_maker.when(r), r["id"]))
    return out


def _candidates(filters: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [int(r["id"]) for r in _query(filters, columns=("id",))]
    if len(ids) > MAX_CANDIDATES:
        step = len(ids) / MAX_CANDIDATES
        ids = [ids[int(i * step)] for i in range(MAX_CANDIDATES)]
    return _rows(filters, ids)


def _count(value: Any) -> int:
    if value is None:
        return maker.DEFAULT_COUNT
    if isinstance(value, bool) or not isinstance(value, int) \
            or not maker.MIN_COUNT <= value <= maker.MAX_COUNT:
        abort(400, description=Said("A film has {min_count} to {max_count} moments.",
                                    min_count=maker.MIN_COUNT, max_count=maker.MAX_COUNT))
    return value


def _story_counts(ids: list[int]) -> dict[int, int]:
    try:
        return {int(a): int(n) for a, n in _in_chunks(
            "SELECT asset_id, COUNT(*) FROM stories WHERE asset_id IN ({marks}) "
            "GROUP BY asset_id", ids)}
    except Exception:                                      # noqa: BLE001 - no stories table yet
        return {}


def _songs() -> list[dict[str, Any]]:
    """Music this viewer could find in the gallery, newest first: not voice
    memos, calls or audiobooks (media/audio_art.py)."""
    from ..media import audio_art                           # noqa: PLC0415

    rows, _ = db.query_assets(_conn(), _roots(), kinds=["audio"], sort="date_desc",
                              limit=MAX_SONGS * 3, offset=0, with_total=False, **_viewer())
    songs = []
    for row in rows:
        if audio_art.classify(row.get("rel_path") or "", duration=row.get("duration")) \
                != audio_art.MUSIC:
            continue
        songs.append({"id": int(row["id"]), "title": Path(row.get("rel_path") or "").stem,
                      "duration": row.get("duration")})
        if len(songs) >= MAX_SONGS:
            break
    return songs


def _music(choice: Any) -> tuple[int | None, str | None]:
    """The song a film is to have: ``{"kind": "library", "id": …}`` for one this
    viewer could find in the gallery, or ``{"kind": "upload", "token": …}`` for
    one they brought. Its library id (for the record) and its file."""
    if choice in (None, {}):
        return None, None
    if not isinstance(choice, dict) or choice.get("kind") not in ("library", "upload"):
        abort(400, description="Choose a song from the library or one you brought.")
    if choice["kind"] == "upload":
        path = store.song_path(_cfg().state_dir, current_user().id, choice.get("token"))
        if path is None or not path.is_file():
            abort(400, description="That song cannot be used.")
        return None, str(path)
    song_id = choice.get("id")
    if isinstance(song_id, bool) or not isinstance(song_id, int) or not 0 < song_id < 2 ** 63:
        abort(400, description="That song cannot be used.")
    rows, _ = db.query_assets(_conn(), _roots(), kinds=["audio"], ids=[song_id], limit=1,
                              offset=0, with_total=False, **_viewer())
    if not rows:
        abort(400, description="That song cannot be used.")
    root = Path(rows[0]["root"]).resolve()
    path = (root / rows[0]["rel_path"]).resolve()
    if not _safe_under(path, root) or not path.is_file():
        abort(409, description="The file of that song cannot be reached.")
    return song_id, str(path)


def _face_boxes(rows: list[dict[str, Any]]) -> dict[int, list[list[float]]]:
    """Each photograph's faces as ``[x, y, w, h]`` of the upright picture, 0 to 1."""
    sizes = {int(r["id"]): (r.get("width") or 0, r.get("height") or 0) for r in rows}
    boxes: dict[int, list[list[float]]] = {}
    for asset_id, bbox in _in_chunks("SELECT asset_id, bbox FROM faces WHERE asset_id IN ({marks})",
                                     list(sizes)):
        w, h = sizes.get(int(asset_id), (0, 0))
        try:
            x, y, bw, bh = (float(v) for v in json.loads(bbox)[:4])
        except (TypeError, ValueError):
            continue
        if w and h and bw > 0 and bh > 0:
            boxes.setdefault(int(asset_id), []).append([x / w, y / h, bw / w, bh / h])
    return boxes


def _story_for(asset_id: int) -> dict[str, Any] | None:
    """The voice story to tell over a photograph: the first one written
    down, or else the first one told. Its words come with it for subtitles."""
    conn = _conn()
    told = story_store.for_asset(conn, asset_id)
    if not told:
        return None
    words: dict[int, dict[str, Any]] = {}
    try:
        for row in conn.execute(
                f"SELECT story_id, language, segments FROM story_words WHERE story_id IN "
                f"({','.join('?' * len(told))})", [int(s["id"]) for s in told]):
            try:
                segments = json.loads(row["segments"] or "[]")
            except ValueError:
                segments = []
            if segments:
                words[int(row["story_id"])] = {"language": row["language"], "segments": segments}
    except Exception:                                      # noqa: BLE001 - no words table yet
        words = {}
    story = next((s for s in told if int(s["id"]) in words), told[0])
    folder = story_store.folder_for(_cfg().state_dir).resolve()
    path = (folder / story["file"]).resolve()
    if not _safe_under(path, folder) or not path.is_file():
        return None
    return {"id": int(story["id"]), "path": str(path), "duration": story.get("duration"),
            **words.get(int(story["id"]), {"language": "", "segments": []})}


@bp.get("/api/films/options")
@require_family
def film_options():
    _switched_on()
    return jsonify({"sizes": list(maker.SIZES), "subtitles": list(maker.SUBTITLES),
                    "paces": list(maker.PACES), "looks": list(maker.LOOKS),
                    "count": {"default": maker.DEFAULT_COUNT, "min": maker.MIN_COUNT,
                              "max": maker.MAX_COUNT},
                    "music": [{"kind": "library", **song} for song in _songs()],
                    "my_music": [{"kind": "upload", "token": song["token"], "title": song["title"]}
                                 for song in store.songs_of(_cfg().state_dir, current_user().id)],
                    "max_song_bytes": store.MAX_SONG_BYTES, "fonts": book_maker.font_report(),
                    "busy": maker.building()})


@bp.post("/api/films/pick")
@require_family
def film_pick():
    """The moments Ninaivu would put in a film of this source."""
    _switched_on()
    filters, meta, data = _source()
    count = _count(data.get("count"))
    rows = _candidates(filters)
    ids = [int(r["id"]) for r in rows]
    picked = maker.pick(rows, count, screens=_screens(ids), faces=_faces(ids),
                        signals=_signals(ids))
    told = _story_counts([int(r["id"]) for r in picked])
    return jsonify({
        "source": meta, "candidates": len(rows),
        "items": [{"id": r["id"], "kind": r.get("kind"), "thumb_v": _thumb_version(r),
                   "date": r.get("date_key"), "width": r.get("width"),
                   "height": r.get("height"), "duration": r.get("duration"),
                   "stories": told.get(int(r["id"]), 0)} for r in picked],
        "fonts": book_maker.font_report(),
    })


@bp.post("/api/films/music")
@require_family
def film_music_add():
    """Bring a song for films: a sound file of up to 40 MB, kept for its owner alone."""
    _switched_on()
    if (request.content_length or 0) > store.MAX_SONG_BYTES + 256 * 1024:
        abort(413, description="A song can be at most 40 MB.")
    upload = request.files.get("audio")
    if upload is None:
        abort(400, description="Choose a sound file.")
    data = upload.stream.read(store.MAX_SONG_BYTES + 1)
    if len(data) > store.MAX_SONG_BYTES:
        abort(413, description="A song can be at most 40 MB.")
    mime = story_store.sniff(data[:64])
    if mime is None:
        abort(400, description="A song has to be a sound recording.")
    title = " ".join(Path(upload.filename or "").stem.split())[:120] or "Song"
    token = store.add_song(_cfg().state_dir, current_user().id, data,
                           story_store.AUDIO_TYPES[mime], title)
    return jsonify({"kind": "upload", "token": token, "title": title}), 201


@bp.delete("/api/films/music/<token>")
@require_family
def film_music_delete(token: str):
    _switched_on()
    if not store.delete_song(_cfg().state_dir, current_user().id, token):
        abort(404)
    return jsonify({"ok": True})


def _describe(film: dict[str, Any]) -> dict[str, Any]:
    live = maker.progress(film["id"])
    state = film["state"]
    if state == "building" and live is None:
        store.set_state(_conn(), film["id"], "error",
                        error="Ninaivu was restarted while this film was being made.")
        state, film["error"] = "error", "interrupted"
    done = state == "done"
    return {
        "id": film["id"], "title": film["title"], "subtitle": film["subtitle"],
        "size": film["size"], "subtitles": film["subtitles"], "moments": len(film["item_ids"]),
        "stories": len(film["story_ids"]), "seconds": film["seconds"], "bytes": film["bytes"],
        "state": state, "error": film["error"], "created_at": film["created_at"],
        "finished_at": film["finished_at"],
        "progress": {"done": (live or {}).get("done", 0), "total": (live or {}).get("total", 0)},
        "play": f"/api/films/{film['id']}/video" if done else None,
        "download": f"/api/films/{film['id']}/video?download=1" if done else None,
    }


@bp.post("/api/films")
@require_family
def film_build():
    """Make a film of the moments chosen, in the background."""
    _switched_on()
    filters, meta, data = _source()
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw or len(raw) > maker.MAX_COUNT or not all(
            isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2 ** 63 for i in raw):
        abort(400, description=Said("Choose 1 to {max_count} moments.", max_count=maker.MAX_COUNT))
    size = data.get("size") or maker.DEFAULT_SIZE
    subtitles = data.get("subtitles") or "both"
    if size not in maker.SIZES or subtitles not in maker.SUBTITLES:
        abort(400, description="Unknown size or subtitles.")
    pace = data.get("pace") or "steady"
    look = data.get("look") or "natural"
    if pace not in maker.PACES or look not in maker.LOOKS:
        abort(400, description="Unknown pace or look.")
    for flag in ("stories", "live"):
        if data.get(flag) not in (None, True, False):
            abort(400, description=Said("{flag} must be true or false", flag=flag))
    months = data.get("months")
    if months is not None and not (isinstance(months, list) and len(months) == 12 and all(
            isinstance(m, str) and 0 < len(m) <= 24 for m in months)):
        abort(400, description="months must be twelve names")
    music_id, music_path = _music(data.get("music"))

    # Only what this viewer may see of this source — whatever ids were sent —
    # in the order they were sent, which is the order they are shown.
    order = list(dict.fromkeys(raw))
    found = {int(r["id"]): r for r in _rows(filters, order)}
    rows = [found[i] for i in order if i in found]
    if not rows:
        abort(400, description="None of those moments can go in a film.")
    boxes = _face_boxes(rows)
    with_stories = data.get("stories") is not False
    with_live = data.get("live") is not False
    items, story_ids = [], []
    for row in rows:
        root = Path(row["root"]).resolve()
        path = (root / row["rel_path"]).resolve()
        if not _safe_under(path, root) or not path.is_file():
            continue
        item = {"id": int(row["id"]), "kind": row.get("kind") or "picture", "path": str(path),
                "rotation": row.get("rotation") or 0, "width": row.get("width"),
                "height": row.get("height"), "duration": row.get("duration"),
                "when": book_maker.when(row), "caption": _human_caption(row),
                "faces": boxes.get(int(row["id"]), [])}
        story = _story_for(item["id"]) if with_stories and item["kind"] == "picture" else None
        if story:
            story_ids.append(story.pop("id"))
            item["story"] = story
        elif with_live and item["kind"] == "picture" and row.get("is_live") \
                and row.get("live_video_path"):
            # A Live Photo moves for its few seconds, as in an iPhone memory;
            # one somebody told a story about stays still under the story.
            moving = (root / row["live_video_path"]).resolve()
            if _safe_under(moving, root) and moving.is_file():
                item.update(kind="live", still=item["path"], path=str(moving), duration=None)
        items.append(item)
    if not items:
        abort(409, description="The files of those moments cannot be reached.")
    title = _text(data, "title", 120) or meta["name"] or "Memory film"
    spec = {"title": title, "subtitle": _text(data, "subtitle", 160),
            "closing": _text(data, "closing", 120), "size": size, "subtitles": subtitles,
            "pace": pace, "look": look, "stories": with_stories, "months": months, "items": items,
            "music": {"path": music_path} if music_path else None}
    if maker.building():
        abort(409, description="A film is already being made. Try again when it is finished.")
    conn = _conn()
    film_id = store.create(conn, owner_id=current_user().id, title=title,
                           subtitle=spec["subtitle"], source_kind=meta["kind"],
                           source_id=meta["id"], size=size, subtitles=subtitles,
                           music_id=music_id, item_ids=[i["id"] for i in items],
                           story_ids=story_ids)
    cfg = _cfg()
    try:
        maker.start(film_id, spec, db_path=cfg.db_path, state_dir=cfg.state_dir)
    except maker.Busy as exc:
        store.delete(conn, cfg.state_dir, store.get(conn, film_id))
        abort(409, description=str(exc))
    return jsonify(_describe(store.get(conn, film_id))), 202


def _mine(film_id: int) -> dict[str, Any]:
    _switched_on()
    user = current_user()
    film = store.get(_conn(), film_id)
    if not store.may_open(film, user.id, user.is_admin):
        abort(404)
    return film


@bp.get("/api/films")
@require_family
def films_list():
    _switched_on()
    return jsonify({"films": [_describe(f) for f in store.listed(_conn(), current_user().id)],
                    "busy": maker.building()})


@bp.get("/api/films/<int:film_id>")
@require_family
def film_status(film_id: int):
    return jsonify(_describe(_mine(film_id)))


@bp.post("/api/films/<int:film_id>/cancel")
@require_family
def film_cancel(film_id: int):
    _mine(film_id)
    return jsonify({"ok": maker.cancel(film_id)})


@bp.get("/api/films/<int:film_id>/video")
@require_family
def film_video(film_id: int):
    """The finished film, to play (with ranges, for seeking) or to download."""
    film = _mine(film_id)
    path = store.file_of(_cfg().state_dir, film)
    if film["state"] != "done" or path is None or not path.is_file():
        abort(404)
    # The film is a copy of the photographs and stories as they were. One since
    # hidden, deleted or erased is not handed out again inside it.
    wanted = [int(i) for i in film["item_ids"]]
    if len(_visible_ids(wanted)) != len(set(wanted)):
        return jsonify({"error": "Some moments in this film can no longer be seen. "
                                 "Make the film again."}), 410
    if film["story_ids"]:
        conn = _conn()
        kept = {int(r[0]) for r in conn.execute(
            f"SELECT id FROM stories WHERE id IN ({','.join('?' * len(film['story_ids']))})",
            film["story_ids"])}
        if kept != set(film["story_ids"]):
            return jsonify({"error": "A story told in this film has been deleted. "
                                     "Make the film again."}), 410
    download = request.args.get("download") == "1"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", film["title"]).strip() or "Memory film"
    return send_file(path, mimetype="video/mp4", as_attachment=download,
                     download_name=f"{name[:80]}.mp4", conditional=True, max_age=0)


@bp.delete("/api/films/<int:film_id>")
@require_family
def film_delete(film_id: int):
    film = _mine(film_id)
    maker.cancel(film_id)
    store.delete(_conn(), _cfg().state_dir, film)
    return jsonify({"ok": True})
