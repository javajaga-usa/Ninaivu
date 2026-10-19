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
import time
from pathlib import Path
from typing import Any

from flask import abort, current_app, jsonify, request, send_file

from ..media import books as book_maker, films as maker, storyboard
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


def _person_faces(person_id: int, rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """One person's face in each photograph: its width as a share of the
    photograph's, its quality, and its eyes (0 to 1 of the upright picture).
    The best of them where the person was found twice in one photograph."""
    sizes = {int(r["id"]): (r.get("width") or 0, r.get("height") or 0) for r in rows}
    found: dict[int, dict[str, Any]] = {}
    conn = _conn()
    ids = list(sizes)
    for start in range(0, len(ids), _CHUNK):
        piece = ids[start:start + _CHUNK]
        for asset_id, bbox, landmarks, quality in conn.execute(
                "SELECT asset_id, bbox, landmarks, quality FROM faces WHERE person_id=? "
                f"AND asset_id IN ({','.join('?' * len(piece))})", (int(person_id), *piece)):
            w, h = sizes.get(int(asset_id), (0, 0))
            try:
                box = [float(v) for v in json.loads(bbox)[:4]]
                points = json.loads(landmarks or "[]")
                eyes = [[float(points[0][0]) / w, float(points[0][1]) / h],
                        [float(points[1][0]) / w, float(points[1][1]) / h]]
            except (TypeError, ValueError, IndexError, ZeroDivisionError):
                continue
            face = {"size": box[2] / w, "quality": float(quality or 0), "eyes": eyes}
            if maker.face_score(face) > maker.face_score(found.get(int(asset_id), {})):
                found[int(asset_id)] = face
    return found


def _person_of(meta: dict[str, Any], style: str) -> int:
    """The person a growing-up or then-and-now film is of."""
    if style != "film" and meta["kind"] != "person":
        abort(400, description="Growing-up and then-and-now films are made of one person: open a person and choose Make a film.")
    return int(meta["id"])


def _style(data: dict[str, Any]) -> str:
    style = data.get("style") or "film"
    if style not in maker.STYLES:
        abort(400, description="Unknown kind of film.")
    return style


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
                    "styles": list(maker.STYLES), "pairs": maker.PAIRS,
                    "writer": _writer() is not None, "busy": maker.building()})


def _writer() -> Any:
    """The model host, when the on-device writing model can draft here."""
    from .. import runtime                                      # noqa: PLC0415
    from ..runtime import llm                                   # noqa: PLC0415
    if not runtime.enabled(_cfg()) or llm.ready() is not None:
        return None
    services = current_app.config.get("MV_SERVICES")
    return services.model_host() if services is not None and hasattr(services, "model_host") else None


@bp.post("/api/films/pick")
@require_family
def film_pick():
    """The moments Ninaivu would put in a film of this source."""
    _switched_on()
    filters, meta, data = _source()
    count = _count(data.get("count"))
    style = _style(data)
    person = _person_of(meta, style)
    rows = _candidates(filters)
    ids = [int(r["id"]) for r in rows]
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if style == "growing_up":
        picked = maker.pick_growing_up(rows, _person_faces(person, rows), count)
    elif style == "then_and_now":
        pairs = maker.pick_then_and_now(rows, _person_faces(person, rows))
        picked = [r for pair in pairs for r in pair]
    else:
        picked = maker.pick(rows, count, screens=_screens(ids), faces=_faces(ids),
                            signals=_signals(ids))
    told = _story_counts([int(r["id"]) for r in picked])

    def shown(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "kind": r.get("kind"), "thumb_v": _thumb_version(r),
                "date": r.get("date_key"), "width": r.get("width"),
                "height": r.get("height"), "duration": r.get("duration"),
                "stories": told.get(int(r["id"]), 0)}

    return jsonify({
        "source": meta, "candidates": len(rows), "style": style,
        "items": [shown(r) for r in picked],
        "pairs": [[shown(a), shown(b)] for a, b in pairs] if style == "then_and_now" else None,
        "fonts": book_maker.font_report(),
    })


@bp.post("/api/films/storyboard")
@require_family
def film_storyboard():
    """A draft of the film's title, chapters and closing line, for the moments
    chosen (media/storyboard.py): by the on-device writing model where the
    household has it, otherwise from dates and places."""
    _switched_on()
    filters, meta, data = _source()
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw or len(raw) > maker.MAX_COUNT or not all(
            isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2 ** 63 for i in raw):
        abort(400, description=Said("Choose 1 to {max_count} moments.", max_count=maker.MAX_COUNT))
    language = data.get("language") if data.get("language") in storyboard.LANGUAGES else "en"
    months = _months(data.get("months"))
    order = list(dict.fromkeys(raw))
    found = {int(r["id"]): r for r in _rows(filters, order)}
    rows = [found[i] for i in order if i in found]
    if not rows:
        abort(400, description="None of those moments can go in a film.")
    from .api_books import _names                               # noqa: PLC0415
    from ..storage import descriptions                          # noqa: PLC0415
    ids = [int(r["id"]) for r in rows]
    people = _names(ids)
    conn = _conn()
    moments = []
    for row in rows:
        asset_id = int(row["id"])
        try:
            about = descriptions.get(conn, asset_id) or ""
        except Exception:                                       # noqa: BLE001 - no table yet
            about = ""
        story = _story_for(asset_id) if (row.get("kind") or "picture") == "picture" else None
        words = ""
        if story:
            key = "english" if language == "en" else "text"
            words = " ".join(str(seg.get(key) or seg.get("text") or "")
                             for seg in story.get("segments") or [])
        moments.append({"id": asset_id, "when": book_maker.when(row), "kind": row.get("kind"),
                        "place": row.get("city") or "", "people": people.get(asset_id, []),
                        "about": _human_caption(row) or about, "story": words})
    board = storyboard.draft(moments, language=language,
                             title=_text(data, "title", 120) or meta.get("name") or "",
                             months=months, host=_writer())
    return jsonify(board)


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


def _months(value: Any) -> list[str] | None:
    if value is not None and not (isinstance(value, list) and len(value) == 12 and all(
            isinstance(m, str) and 0 < len(m) <= 24 for m in value)):
        abort(400, description="months must be twelve names")
    return value


def _chapters(value: Any) -> list[dict[str, Any]]:
    """A film's chapters as the Make a film screen sends them: each starting
    at a moment (its id), with a heading and a line."""
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > storyboard.MOST_CHAPTERS * 2:
        abort(400, description="Chapters are a list, each with the moment it starts at and a heading.")
    out = []
    for chapter in value:
        if not isinstance(chapter, dict) or isinstance(chapter.get("id"), bool) \
                or not isinstance(chapter.get("id"), int):
            abort(400, description="Chapters are a list, each with the moment it starts at and a heading.")
        title = " ".join(str(chapter.get("title") or "").split())[:storyboard.HEADING_CHARS]
        if title:
            out.append({"id": chapter["id"], "title": title,
                        "line": " ".join(str(chapter.get("line") or "").split())[:storyboard.LINE_CHARS]})
    return out


def _held_item(row: dict[str, Any], path: Path, eyes: list[list[float]] | None,
               label: str) -> dict[str, Any]:
    return {"id": int(row["id"]), "kind": "picture", "path": str(path),
            "rotation": row.get("rotation") or 0, "width": row.get("width"),
            "height": row.get("height"), "when": book_maker.when(row), "eyes": eyes,
            "label": label, "faces": []}


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
    months = _months(data.get("months"))
    style = _style(data)
    person = _person_of(meta, style)
    born = data.get("born")
    this_year = time.gmtime().tm_year
    if born is not None and (isinstance(born, bool) or not isinstance(born, int)
                             or not 1880 <= born <= this_year):
        abort(400, description="The year somebody was born has to be a year.")
    age_label = data.get("age_label") or "Age {age}"
    if not isinstance(age_label, str) or len(age_label) > 40 or "{age}" not in age_label:
        abort(400, description="age_label must hold {age}")
    chapters_asked = _chapters(data.get("chapters"))
    music_id, music_path = _music(data.get("music"))

    # Only what this viewer may see of this source — whatever ids were sent —
    # in the order they were sent, which is the order they are shown.
    order = list(dict.fromkeys(raw))
    found = {int(r["id"]): r for r in _rows(filters, order)}
    rows = [found[i] for i in order if i in found]
    if not rows:
        abort(400, description="None of those moments can go in a film.")
    if style == "then_and_now" and len(order) % 2:
        abort(400, description="A then-and-now film is made of pairs.")
    boxes = _face_boxes(rows)
    with_stories = data.get("stories") is not False
    with_live = data.get("live") is not False
    items, story_ids = [], []
    if style != "film":
        # One person held by the eyes: their face in each photograph, and the
        # year (or their age in it) to write under it.
        faces = _person_faces(person, rows)

        def held(asset_id: int) -> dict[str, Any] | None:
            row = found.get(asset_id)
            face = faces.get(asset_id)
            if row is None or face is None:
                return None
            root = Path(row["root"]).resolve()
            path = (root / row["rel_path"]).resolve()
            if not _safe_under(path, root) or not path.is_file():
                return None
            when = book_maker.when(row)
            year = time.gmtime(when).tm_year if when else None
            label = str(year or "")
            if born and year and year >= born:
                label = age_label.replace("{age}", str(year - born))
            return _held_item(row, path, face["eyes"], label)

        if style == "then_and_now":
            for then_id, now_id in zip(order[::2], order[1::2]):
                then, now = held(then_id), held(now_id)
                if then and now:
                    items.append({"id": then["id"], "kind": "pair", "then": then, "now": now})
        else:
            items = [item for item in (held(i) for i in order) if item]
        rows = []
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
    at = {item["id"]: n for n, item in enumerate(items)}
    chapters = [{"at": at[c["id"]], "title": c["title"], "line": c["line"]}
                for c in chapters_asked if c["id"] in at] if style == "film" else []
    spec = {"title": title, "subtitle": _text(data, "subtitle", 160),
            "closing": _text(data, "closing", 120), "size": size, "subtitles": subtitles,
            "pace": pace, "look": look, "stories": with_stories, "months": months, "items": items,
            "style": style, "chapters": chapters,
            "music": {"path": music_path} if music_path else None}
    if maker.building():
        abort(409, description="A film is already being made. Try again when it is finished.")
    conn = _conn()
    film_id = store.create(conn, owner_id=current_user().id, title=title,
                           subtitle=spec["subtitle"], source_kind=meta["kind"],
                           source_id=meta["id"], size=size, subtitles=subtitles,
                           music_id=music_id,
                           item_ids=[n for i in items for n in
                                     ((i["then"]["id"], i["now"]["id"]) if i["kind"] == "pair"
                                      else (i["id"],))],
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
