"""Ask Ninaivu: a question about the family's photographs, answered here.

Off until ``ask_ninaivu`` is switched on, when every route answers 404 as if
it did not exist. Family members and administrators may ask; guests, who are
never told who is in a photograph or where it was taken, may not.

``POST /api/ask-ninaivu`` with ``{"question": "…"}`` reads the question
(ask/question.py), finds the photographs it is about inside the asker's own
limits, exactly as the gallery's search would (``api._viewer``,
``api._semantic_ids``), and answers (ask/answer.py): always plainly, as
sentences the page puts in its own language, and in words written by the
on-device writing model when the AI engine is on and that model is
downloaded (runtime/llm.py). Nothing leaves the computer either way.

``GET /api/ask-ninaivu`` says whether it is on and whether answers will be
written by the model, so the page can say so before anybody asks.
"""
from __future__ import annotations

import calendar
import logging
import sqlite3
import threading
import time
from typing import Any

from flask import abort, current_app, jsonify

from .. import runtime
from ..ask import answer as answer_mod
from ..ask import question as question_mod
from ..server.auth import current_user, require_family
from ..storage import db, journal
from ..words import Said
from ._body import json_object
from .api import (_cfg, _conn, _phrase_vocabulary, _public, _roots, _semantic_ids, _viewer,
                  bp)

log = logging.getLogger(__name__)

#: The longest question, in characters.
MAX_QUESTION = 500
#: Seconds the writing model is given for each of its two calls.
READ_SECONDS = 60.0
WRITE_SECONDS = 150.0
#: Seconds a question waits for the model host to finish another call before
#: it is answered in plain sentences instead (audit C-12).
HOST_WAIT = 10.0
#: "What is new" without dates means this many days.
NEW_DAYS = 7
#: Questions answered at once. The model writes one answer at a time anyway;
#: more than this waiting is a household asking faster than it can answer.
_asking = threading.BoundedSemaphore(3)


def _switched_on() -> None:
    if not getattr(_cfg(), "ask_ninaivu", False):
        abort(404)
    if current_user().is_guest:
        abort(404)


def _writer() -> tuple[bool, str]:
    """Whether the writing model will write the answers, and if not, why."""
    cfg = _cfg()
    if not runtime.enabled(cfg):
        return False, "the on-device AI engine is off (Advanced settings)"
    from ..runtime import llm                                   # noqa: PLC0415
    why = llm.ready()
    return why is None, why or ""


def _host():
    services = current_app.config.get("MV_SERVICES")
    return services.model_host() if services is not None else None


@bp.get("/api/ask-ninaivu")
@require_family
def ask_status():
    _switched_on()
    writes, why = _writer()
    return jsonify({"on": True, "writer": writes, "why": why,
                    "journal": journal.enabled(_cfg()), "max_question": MAX_QUESTION})


@bp.post("/api/ask-ninaivu")
@require_family
def ask():
    _switched_on()
    data = json_object()
    asked = str(data.get("question") or "").strip()
    if not asked:
        return jsonify({"error": "Type a question first."}), 400
    if len(asked) > MAX_QUESTION:
        return jsonify({"error": Said("A question can be at most {max} characters.",
                                      max=MAX_QUESTION)}), 400
    if not _asking.acquire(timeout=1.0):
        return jsonify({"error": "Ninaivu is answering other questions; ask again in a moment."}), 429
    try:
        return jsonify(_answer(asked, plain=bool(data.get("plain"))))
    finally:
        _asking.release()


def _answer(asked: str, *, plain: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    cfg = _cfg()
    conn = _conn()
    roots = _roots()
    viewer = _viewer()
    user = current_user()
    people, places = _phrase_vocabulary()
    q = question_mod.understand(asked, people=people, places=places,
                                southern=getattr(cfg, "southern_hemisphere", False))
    writes, _ = _writer()
    writes = writes and not plain
    host = _host() if writes else None
    model = ""
    if host is not None and (q.language == "ta" or not (q.people or q.place or q.window)):
        # The rules read an English question well; the model reads Tamil, and
        # questions the rules found nothing in, better.
        try:
            planned = host.call("generate", messages=question_mod.plan_messages(q, people, places),
                                max_tokens=200, temperature=0.0,
                                json_schema=question_mod.PLAN_SCHEMA, timeout=READ_SECONDS,
                                wait=HOST_WAIT)
            from ..runtime import llm                           # noqa: PLC0415
            q = question_mod.merge_plan(q, llm.parse_json(planned.get("text") or ""),
                                        people=people, places=places)
        except runtime.HostError:
            # Not there, busy, or it ran out of time (and was stopped): the
            # answer would only wait as long again, or longer with the model
            # loaded afresh, so it is given plainly (audit D-13).
            log.info("the writing model could not read a question; answering plainly",
                     exc_info=True)
            host = None
        except Exception:                                       # noqa: BLE001 - the rules' reading stands
            log.info("the writing model could not read a question", exc_info=True)

    since: float | None = None
    only: list[int] | None = None
    if q.intent == "new":
        since = (_start_of(q.window.date_from)
                 if q.window is not None else time.time() - NEW_DAYS * 86400)
        # Taken, for "what is new": when it arrived, not when it was taken.
        q.window = None
        if journal.enabled(cfg):
            only = _new_for(conn, roots, user, lambda offset: journal.added_since(
                conn, since, limit=NEW_POOL, offset=offset, roots=roots))
        else:
            only = _new_for(conn, roots, user, lambda offset: _added_lately(
                conn, roots, since, offset=offset))

    ranked, _ = _semantic_ids(q.text, roots, conn) if q.text else (None, {})
    found = answer_mod.find(conn, roots, viewer, q, ranked=ranked, only=only,
                            people_names={pid: name for pid, name in people},
                            include_nsfw=user.sees_explicit, guest=user.is_guest)
    lines = answer_mod.plain_lines(q, found, since=since)
    written = ""
    cited: list[int] = []
    if host is not None and (found.moments or found.total or q.intent == "new"):
        try:
            reply = host.call("generate",
                              messages=answer_mod.model_messages(
                                  q, found, since=since,
                                  household=getattr(cfg, "house_name", "") or ""),
                              max_tokens=400, temperature=0.3, timeout=WRITE_SECONDS,
                              wait=HOST_WAIT)
            written, cited = answer_mod.checked(reply.get("text") or "",
                                                [m["id"] for m in found.moments])
            model = str(reply.get("model") or "")
        except runtime.Unavailable:
            pass
        except Exception:                                       # noqa: BLE001 - the plain answer stands
            log.info("the writing model could not answer a question", exc_info=True)
    rows = found.rows
    if cited:
        first = {aid: i for i, aid in enumerate(cited)}
        rows = sorted(rows, key=lambda r: first.get(r["id"], len(first)))
    return {
        "question": q.asked,
        "language": q.language,
        "intent": q.intent,
        "understood": q.chips(),
        "lines": lines,
        "written": written,
        "model": model,
        "cited": cited,
        "total": found.total,
        "items": [_public(r) for r in rows],
        "ids": found.ids[:2000],
        "took": answer_mod.took(started),
    }


#: How many of the newest additions the asker may see go to the answer.
NEW_POOL = 2000


def _start_of(day: str) -> float:
    """Unix seconds at the start of *day* here. ``time.mktime`` refuses a
    day before 1970 on Windows (D-30), so such a day is read as UTC: it is
    before anything the library can have had added anyway."""
    parsed = time.strptime(day, "%Y-%m-%d")
    try:
        return time.mktime(parsed)
    except (OverflowError, ValueError, OSError):
        return float(calendar.timegm(parsed))


def _new_for(conn, roots, user, page) -> list[int]:  # noqa: ANN001
    """The newest :data:`NEW_POOL` additions *user* may see, from *page*
    (newest first, by offset). Taking the newest 2,000 across the library
    before asking who may see them told a family member nothing was new
    when the household's own latest were all hidden from them (D-29)."""
    viewer = _viewer()
    out: list[int] = []
    offset = 0
    while len(out) < NEW_POOL:
        batch = page(offset)
        if not batch:
            break
        rows, _ = db.query_assets(conn, roots, ids=batch, limit=len(batch), offset=0,
                                  columns=("id",), include_nsfw=user.sees_explicit,
                                  guest_search=user.is_guest, include_companions=False,
                                  with_total=False, **viewer)
        seen = {int(r["id"]) for r in rows}
        out.extend(i for i in batch if i in seen)
        offset += len(batch)
        if len(batch) < NEW_POOL:
            break
    return out[:NEW_POOL]


def _added_lately(conn, roots, since: float, *, offset: int = 0) -> list[int]:  # noqa: ANN001
    """Without the change journal, what is new is what was first indexed
    since *since* (``assets.added_at``; an edit, a turn or a redrawn tile
    moves ``indexed_at`` but not this)."""
    marks = ",".join("?" * len(roots)) or "''"
    try:
        rows = conn.execute(
            f"SELECT id FROM assets WHERE root IN ({marks}) AND trashed = 0 "
            "AND COALESCE(added_at, indexed_at, 0) >= ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (*roots, since, NEW_POOL, int(offset))).fetchall()
    except sqlite3.Error:
        log.warning("could not list what was added lately", exc_info=True)
        return []
    return [int(r[0]) for r in rows]
