"""Running face detection and grouping over a library.

The three pieces below it stay deliberately separate — :mod:`ninaivu.faces`
knows about pixels, :mod:`ninaivu.facematch` knows about vectors, and
:mod:`ninaivu.db` knows about rows. This module is the only place that knows
about all three, which is what keeps each of them testable on its own.

The work happens in two passes, and they are separate for a practical reason:
detection is slow and per-file, grouping is fast and needs to see everything
at once. Detection is resumable and survives being stopped; grouping is cheap
enough to redo from scratch whenever anything changes.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from ..storage import db
from . import faces as faces_mod, facematch

log = logging.getLogger("ninaivu.faceindex")

#: Faces below this are stored but never clustered or suggested. They are kept
#: rather than dropped so that "how many faces are in this photograph" stays
#: honest, and so a later detector version can reconsider them.
CLUSTER_MIN_QUALITY = 0.15

#: One regroup at a time, across every indexer in the process. Two at once —
#: the scan's own at the end of a pass and one the console started, say —
#: each read the loose faces, then wrote their answers over each other's, and
#: labelled their groups from the same clock second, so two different groups
#: could share a key and be named as one person.
_REGROUP_LOCK = threading.Lock()


def readers_for(cfg: Any) -> int:
    """How many originals the face pass opens ahead of the detector.

    Sized by the Tuning page's scan workers (resources.helpers): up to four
    as many as the workers, half of them beyond that, never more than resources.MAX_HELPERS
    (each is a full-size original held as raw pixels). Power saving, or a Pi's small
    profile, asked for one or two things at once; this pass used to read two
    to four ahead from the processor count whatever was chosen.
    """
    try:
        tuned = int(getattr(cfg, "workers", 0) or 0)
    except (TypeError, ValueError):
        tuned = 0
    if tuned <= 0:
        return min(4, max(2, (os.cpu_count() or 4) // 2))
    from ..utils.resources import helpers                     # noqa: PLC0415
    return helpers(tuned)


def finders_for(cfg: Any) -> int:
    """How many threads open originals *and* find the faces in them.

    As many as the scan's workers, the same count indexing uses: each holds
    one original as raw pixels while it works, as each indexing worker does,
    so the Tuning page's memory plan already covers it. The readers above
    stop at resources.MAX_HELPERS because they only open files ahead of one detector; these
    do the detecting, and eight of them left twelve of a twenty-core
    machine's cores idle.
    """
    try:
        tuned = int(getattr(cfg, "workers", 0) or 0)
    except (TypeError, ValueError):
        tuned = 0
    if tuned <= 0:
        return readers_for(cfg)
    # Each finder also holds its own models, about 140 MB with what detection
    # works in, which the plan does not count: no more of them than half of
    # the memory free right now holds, a photograph's worth beside each.
    try:
        import psutil                                     # noqa: PLC0415
        free = int(psutil.virtual_memory().available)
    except Exception:                                     # noqa: BLE001
        return tuned
    room = max(1, free // 2 // FINDER_BYTES)
    return max(1, min(tuned, room))


#: What one face finder holds: its models and working memory (measured at
#: about 140 MB) and the original it is looking at.
FINDER_BYTES = 300 * 1024 * 1024


class _Found:
    """The faces a reading thread found itself (see detect_pass)."""

    __slots__ = ("faces",)

    def __init__(self, faces) -> None:
        self.faces = faces


class _Stopped(Exception):
    """A regroup asked to stop part way."""


class FaceIndexer:
    """Detection and grouping for one library."""

    def __init__(self, cfg: Any, engine: faces_mod.FaceEngine | None = None) -> None:
        self.cfg = cfg
        self.engine = engine or faces_mod.FaceEngine(cfg.state_dir, model=face_model(cfg))
        self.crops_dir = Path(cfg.state_dir) / "faces"

    def is_current(self) -> bool:
        """Whether this indexer's recogniser is still the one the
        ``face_model`` setting names. A kept indexer that is not would look at
        every photograph again with the old pair and stamp each one done, so
        the new one never ran (audit C-02)."""
        want = faces_mod.model_id_for(face_model(self.cfg))
        have = getattr(self.engine, "model_id", None)
        return have is None or have == want

    # -- detection --------------------------------------------------------
    def detect_pass(
        self,
        conn,
        root: str,
        *,
        limit: int = 0,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Find faces in every picture that has not been looked at yet.

        Detection reads the *original*, not the thumbnail. Thumbnails top out
        at 640px here, and a face in a group shot is then perhaps thirty
        pixels across — below what any recogniser can identify, and squarely
        in the range where embeddings cluster by blur rather than by person.
        """
        if not self.engine.available:
            return {"ok": False, "reason": self.engine.unavailable_reason,
                    "scanned": 0, "faces": 0}

        version = faces_mod.FACE_VERSION
        pending = db.assets_needing_faces(conn, root, version, limit=limit)
        total = len(pending)
        scanned = found = failed = 0
        stopped = False
        if not pending:
            return {"ok": True, "scanned": 0, "faces": 0, "failed": 0, "remaining": 0}

        missing = object()
        # Each reading thread finds the faces in what it opened, with its own
        # copy of the models, when the engine is the real one. The one shared
        # engine is locked around every call (OpenCV's models are not safe
        # from two threads), and the scan's own OpenCV runs on one core
        # (upright), so every face in the library was found on one core while
        # the rest of a large machine waited. Fakes and anything else handed
        # in are still asked from this thread only, as they always were.
        real = type(self.engine) is faces_mod.FaceEngine
        finders = finders_for(self.cfg) if real else 0
        parallel = finders > 1
        if parallel:
            # One core each, as the scan's other OpenCV work already is
            # (upright._face_cascades): left to fan out too, every finder
            # spread over every core and they spent the machine waiting on
            # one another.
            try:
                faces_mod.cv2.setNumThreads(1)
            except Exception:                              # noqa: BLE001
                pass
        own = threading.local()

        def _engine_here() -> faces_mod.FaceEngine:
            engine = getattr(own, "engine", None)
            if engine is None:
                # The same pair as the shared engine; the default pair asks
                # for nothing, as it always has.
                chosen = getattr(self.engine, "model", "sface")
                spare = {"model": chosen} if chosen != "sface" else {}
                engine = own.engine = faces_mod.FaceEngine(self.engine.state_dir, **spare)
                if not engine.available:
                    engine = own.engine = self.engine
            return engine

        def _load_image(p: Path, rot: int):
            if not p.exists():
                return missing
            image = faces_mod.load_image_for_faces(p, rot)
            if not parallel or image is None:
                return image
            # Found here and the pixels let go here: only the faces travel.
            return _Found(_engine_here().detect(image))

        if parallel:
            workers = finders
        elif real:
            # Room for one finder only: the free memory that kept the finders
            # to one does not hold eight full-size originals queued ahead of
            # the detector either.
            workers = max(1, min(readers_for(self.cfg), finders))
        else:
            workers = readers_for(self.cfg)
        # Each decode is a full-size original held as raw pixels, so only a
        # worker's worth waits ahead: on 48-megapixel files, twice that was
        # well over a gigabyte sitting in the queue.
        lookahead = workers
        pending_iter = iter(pending)
        inflight: deque[tuple[dict[str, Any], Any]] = deque()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in range(lookahead):
                try:
                    row = next(pending_iter)
                except StopIteration:
                    break
                p = Path(root) / row["rel_path"]
                rot = int(row.get("rotation") or 0)
                inflight.append((row, pool.submit(_load_image, p, rot)))

            while inflight:
                if should_stop and should_stop():
                    for _, fut in inflight:
                        fut.cancel()
                    stopped = True
                    break

                row, fut = inflight.popleft()

                try:
                    next_row = next(pending_iter)
                    np_path = Path(root) / next_row["rel_path"]
                    np_rot = int(next_row.get("rotation") or 0)
                    inflight.append((next_row, pool.submit(_load_image, np_path, np_rot)))
                except StopIteration:
                    pass

                try:
                    image = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.debug("face detection failed on %s: %s", row.get("rel_path"), exc)
                    image = None
                if image is missing:
                    # Not there is not unreadable. A library drive unplugged
                    # or asleep part way made every photograph after it read
                    # as missing, and each was marked done with no faces, for
                    # good: unchanged when the drive came back, none was ever
                    # looked at again. The pass stops when the library has
                    # gone; a single file deleted is left for the scan to tidy.
                    if not Path(root).is_dir():
                        log.warning("face pass: %s is not there any more; stopping", root)
                        for _, waiting in inflight:
                            waiting.cancel()
                        stopped = True
                        break
                    continue
                if isinstance(image, _Found):
                    detected = image.faces
                else:
                    try:
                        detected = self.engine.detect(image) if image is not None else None
                    except Exception as exc:  # noqa: BLE001
                        log.debug("face detection failed on %s: %s",
                                  row.get("rel_path"), exc)
                        detected = None

                if detected is None:
                    # Unreadable, or not an image any more. Mark it done anyway,
                    # or every future pass retries the same broken file forever.
                    db.mark_faces_scanned(conn, int(row["id"]), version)
                    failed += 1
                else:
                    stored = self._store(conn, int(row["id"]), detected)
                    found += stored
                scanned += 1
                if on_progress and scanned % 10 == 0:
                    on_progress(scanned, total)

        if on_progress:
            on_progress(scanned, total)
        return {"ok": True, "scanned": scanned, "faces": found,
                "failed": failed, "remaining": max(0, total - scanned),
                "stopped": stopped}

    def _detect_one(self, path: Path, rotation: int):
        if not path.exists():
            return None
        image = faces_mod.load_image_for_faces(path, rotation)
        if image is None:
            return None
        return self.engine.detect(image)

    def _store(self, conn, asset_id: int, detected: Sequence) -> int:
        if not db.ai_may_read(conn, asset_id):
            # Hidden while this pass was working: no crops of it on disk.
            return 0
        rows: list[dict[str, Any]] = []
        for index, face in enumerate(detected):
            record = face.as_row()
            record["embedding"] = faces_mod.pack(face.embedding)
            name = f"{asset_id}_{index}.jpg"
            if faces_mod.write_crop(face.crop, self.crops_dir / name):
                record["thumb"] = name
            rows.append(record)
        return db.replace_asset_faces(conn, asset_id, rows, self.engine.model_id)

    # -- grouping ---------------------------------------------------------
    def regroup(self, conn, roots: Sequence[str] | str, *,
                should_stop: Callable[[], bool] | None = None,
                planner: Callable[[Sequence[str] | str, Callable[[], bool]],
                                  dict[str, Any]] | None = None) -> dict[str, Any]:
        """Re-cluster loose faces and extend named people onto new ones.

        Order matters. Named people are matched *first*, so a face that
        clearly belongs to somebody already named never ends up founding an
        anonymous cluster the admin then has to merge by hand.

        *should_stop* is asked as it goes. On a large library the grouping
        takes minutes, and Stop used to wait for all of it. A regroup that
        stops writes nothing it has not finished and answers
        ``{"stopped": True}``; the next one starts again from the faces.

        The reading and the working out (:func:`plan_regroup`) write
        nothing; *planner* runs them somewhere else, given the roots and
        *should_stop* (the job engine's worker process, so minutes of matching
        do not share the server's interpreter with the gallery), and raises
        :class:`_Stopped` to stop. What it worked out is written here.
        """
        with _REGROUP_LOCK:
            try:
                return self._regroup(conn, roots, should_stop or (lambda: False), planner)
            except _Stopped:
                return {"stopped": True}

    def _regroup(self, conn, roots: Sequence[str] | str, should_stop: Callable[[], bool],
                 planner: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
        if planner is not None:
            plan = planner(roots, should_stop)
        else:
            if should_stop():
                raise _Stopped
            plan = plan_regroup(conn, roots, should_stop, people=self._load_people(conn),
                                model_id=self.model_id)
        return self._apply(conn, plan, should_stop)

    def _apply(self, conn, plan: dict[str, Any],
               should_stop: Callable[[], bool]) -> dict[str, Any]:
        """Write what :func:`plan_regroup` worked out."""
        if should_stop():
            raise _Stopped
        db.set_faces_person(conn, plan["assigned"])
        db.save_cluster_keys(conn, plan["pairs"])
        db.hide_faces(conn, plan.get("hide", ()))
        for person_id in plan["people"]:
            if should_stop():
                # Each person's centre is rebuilt from their confirmed faces
                # only, which this regroup did not change: what is left is
                # their counts and covers, which the next regroup refreshes.
                break
            self.refresh_person(conn, person_id)
        return {"auto_assigned": len(plan["assigned"]), "suggested": plan["suggested"],
                "clusters": plan["clusters"], "loose_faces": plan["loose_faces"]}

    # -- people -----------------------------------------------------------
    def _load_people(self, conn, only: int | None = None) -> list[facematch.Person]:
        return load_people(conn, only, model_id=self.model_id)

    @staticmethod
    def _exemplars(vectors: Sequence) -> list:
        return _exemplars(vectors)

    def refresh_person(self, conn, person_id: int) -> None:
        """Rebuild one person's centroid from their confirmed faces."""
        faces = db.load_faces(conn, person_id=person_id)
        confirmed = [f for f in faces if f["source"] == "confirmed"]
        vectors = [v for v in (faces_mod.unpack(f["embedding"], self.embed_dim)
                               for f in confirmed) if v is not None]
        centre = facematch.centroid(vectors) if vectors else None
        cover = None
        if faces:
            cover = int(max(faces, key=lambda f: float(f["quality"] or 0))["id"])
        db.save_person_centroid(
            conn, person_id,
            faces_mod.pack(centre) if centre is not None else None,
            len(confirmed), len(faces), cover)

    def confirm(self, conn, face_id: int, person_id: int) -> dict[str, Any]:
        """Attach a face to a person because a human said so."""
        db.set_face_person(conn, face_id, person_id,
                           source="confirmed", confidence=1.0)
        self.refresh_person(conn, person_id)
        return {"face_id": face_id, "person_id": person_id, "source": "confirmed"}

    def reject(self, conn, face_id: int, person_id: int) -> dict[str, Any]:
        db.reject_face_for_person(conn, face_id, person_id)
        self.refresh_person(conn, person_id)
        return {"face_id": face_id, "person_id": person_id, "rejected": True}

    def name_cluster(self, conn, cluster_key: str, name: str) -> dict[str, Any]:
        """Turn an anonymous cluster into a named person.

        Every face in the cluster becomes *confirmed*, because naming a
        cluster is a human looking at it and saying yes. That is what makes
        the first naming immediately useful: the centroid it produces is built
        from real confirmations, so the very next regroup can extend the
        person across the rest of the library.

        The name is matched case-insensitively against everybody already
        named, so typing an existing name joins that person rather than
        creating a second one who happens to share it — ``joined_existing``
        says which happened, for the console to report accurately.

        A cluster with no unnamed faces left raises LookupError and names
        nobody. Cluster keys are reissued by every regroup — and naming one
        group starts a regroup — so a card still on screen can carry a key that
        no longer exists. Creating the person anyway left a name with no faces
        behind it: counted as a person, shown nowhere.
        """
        # Every face in the group, not a page of them: naming a group of 1,200
        # confirmed its best 500 and quietly left the rest unnamed.
        face_ids = db.cluster_face_ids(conn, cluster_key, limit=-1)
        if not face_ids:
            raise LookupError(cluster_key)
        existing = conn.execute(
            "SELECT id FROM people_clusters WHERE name=? COLLATE NOCASE", (name,)
        ).fetchone()
        person = db.create_or_update_person_cluster(conn, name)
        person_id = int(person["id"])
        db.set_faces_person(conn, [(face_id, person_id, "confirmed", 1.0)
                                   for face_id in face_ids])
        self.refresh_person(conn, person_id)
        return {"person": person, "faces": len(face_ids),
                "joined_existing": bool(existing)}

    def suggestions(self, conn, person_id: int, roots: Sequence[str] | str,
                    *, limit: int = 60) -> list[dict[str, Any]]:
        """The review queue for one person, best guesses first."""
        people = {p.person_id: p for p in self._load_people(conn, only=int(person_id))}
        person = people.get(int(person_id))
        if person is None:
            return []
        loose = db.load_faces(conn, unassigned=True, roots=roots,
                              min_quality=CLUSTER_MIN_QUALITY)
        candidates = []
        for row in loose:
            vector = faces_mod.unpack(row["embedding"], self.embed_dim)
            if vector is not None:
                candidates.append(facematch.Candidate(
                    face_id=int(row["id"]), vector=vector,
                    quality=float(row["quality"] or 0),
                    asset_id=int(row["asset_id"])))
        hidden = {int(r["id"]) for r in loose if r.get("hidden")}
        if hidden:
            candidates = [c for c in candidates if c.face_id not in hidden]
        blocked = db.rejections(conn, person_id=int(person_id))
        ranked = facematch.rank_for_person(person, candidates, blocked=blocked,
                                           floor=self.thresholds.suggest)

        by_id = {int(r["id"]): r for r in loose}
        out = []
        for face_id, score in ranked[:limit]:
            row = by_id.get(face_id)
            if not row:
                continue
            try:
                bbox = json.loads(row["bbox"])
            except (TypeError, ValueError):
                bbox = []
            out.append({
                "face_id": face_id,
                "asset_id": int(row["asset_id"]),
                "score": round(score, 4),
                "quality": round(float(row["quality"] or 0), 4),
                "thumb": row["thumb"],
                "bbox": bbox,
            })
        return out

    # -- reporting --------------------------------------------------------
    def status(self, conn, root: str) -> dict[str, Any]:
        stats = db.face_stats(conn, root)
        stats["engine"] = self.engine.info
        stats["models"] = faces_mod.model_status(self.cfg.state_dir,
                                                 getattr(self.engine, "model", "sface"))
        stats["thresholds"] = self.thresholds.as_dict()
        return stats

    # -- which recogniser -------------------------------------------------
    @property
    def model_id(self) -> str | None:
        """The engine's recogniser; ``None`` (the table's majority) for an
        indexer made without one, as the regroup written from a worker's
        plan is."""
        return getattr(getattr(self, "engine", None), "model_id", None)

    @property
    def embed_dim(self) -> int | None:
        engine = getattr(self, "engine", None)
        if engine is None:
            return None
        return int(getattr(engine, "embed_dim", None) or faces_mod.embed_dim_of(self.model_id))

    @property
    def thresholds(self) -> facematch.Thresholds:
        return facematch.thresholds_for(self.model_id)


def face_model(cfg: Any) -> str:
    """The ``face_model`` setting: which pair of models finds and tells faces
    apart, ``sface`` unless the household chose ``arcface``."""
    return str(getattr(cfg, "face_model", "") or "sface")


def load_people(conn, only: int | None = None,
                model_id: str | None = None) -> list[facematch.Person]:
    """Named people, described only by faces a human confirmed.

    Faces of the recogniser *model_id* (the engine's; the one most of the
    table was made by when not given): after a switch of ``face_model`` the
    table holds both recognisers' rows until every photograph has been
    looked at again, and the two are not comparable.

    Automatic assignments are deliberately excluded from the centroid. If
    they fed back in, one wrong guess would shift the person's centre
    toward the wrong face, which makes the next wrong guess more likely —
    the failure mode where a person's album slowly fills with a stranger
    and every step looked reasonable.

    One query for everybody's confirmed faces, not one per person: with a
    hundred and fifty people that was 150 queries reading every automatic
    face too, only to throw them away. *only* builds just that person, for
    the one review queue on screen.
    """
    dim = faces_mod.embed_dim_of(model_id or db.face_model_in_use(conn))
    confirmed: dict[int, list] = {}
    for face in db.load_faces(conn, person_id=only, confirmed_only=True):
        vector = faces_mod.unpack(face["embedding"], dim)
        if vector is not None:
            # Best first within each person, as the query hands them over.
            confirmed.setdefault(int(face["person_id"]), []).append(vector)
    people: list[facematch.Person] = []
    for row in db.list_people_clusters(conn):
        person_id = int(row["id"])
        vectors = confirmed.get(person_id)
        if not vectors:
            continue
        centre = facematch.centroid(vectors)
        if centre is None:
            continue
        exemplars = _exemplars(vectors)
        people.append(facematch.Person(person_id, centre, exemplars,
                                       row["name"]))
    return people


def _exemplars(vectors: Sequence) -> list:
    chosen: list = []
    for vector in vectors:
        facematch._update_exemplars(chosen, facematch.unit(vector))
    return chosen


def plan_regroup(conn, roots: Sequence[str] | str,
                 should_stop: Callable[[], bool] = lambda: False,
                 people: list[facematch.Person] | None = None,
                 model_id: str | None = None) -> dict[str, Any]:
    """What a regroup would write, worked out from the index without writing.

    Plain values only, so a worker process can work it out and hand it back:
    ``assigned`` (``(face id, person id, "auto", score)``), ``pairs`` (``(face
    id, cluster key or None)``), ``hide`` (new faces of a group somebody hid
    from naming), ``people`` (whose counts to refresh) and the counts. Raises :class:`_Stopped` when *should_stop* says so. *people*
    are the named people (:func:`load_people`), read here when not given.
    *model_id* is the recogniser whose faces are grouped, with its own
    thresholds (:func:`facematch.thresholds_for`); the one most of the table
    was made by when not given.
    """
    def check() -> None:
        if should_stop():
            raise _Stopped

    check()
    if model_id is None:
        model_id = db.face_model_in_use(conn)
    dim = faces_mod.embed_dim_of(model_id)
    rules = facematch.thresholds_for(model_id)
    if people is None:
        people = load_people(conn, model_id=model_id)
    suggested = 0
    assigned: list[tuple[int, int, str, float]] = []

    loose = db.load_faces(conn, unassigned=True, roots=roots,
                          min_quality=CLUSTER_MIN_QUALITY)
    # Faces somebody hid from naming are never given to a named person: a
    # stranger in the background should not drift into somebody's album.
    hidden = {int(row["id"]) for row in loose if row.get("hidden")}
    if people:
        # Every rejection at once rather than a query per loose face.
        blocked_for: dict[int, list[int]] = {}
        for face_id, person_id in db.rejections(conn):
            blocked_for.setdefault(face_id, []).append(person_id)
        for number, row in enumerate(loose):
            if number % 2000 == 0:
                check()
            if int(row["id"]) in hidden:
                continue
            vector = faces_mod.unpack(row["embedding"], dim)
            if vector is None:
                continue
            result = facematch.assign_face(
                vector, people, blocked=blocked_for.get(int(row["id"]), ()),
                auto=rules.auto, suggest=rules.suggest, margin=rules.margin, link=rules.link)
            if result.decision == "auto" and result.person_id:
                assigned.append((int(row["id"]), result.person_id,
                                 "auto", result.score))
            elif result.decision == "suggest":
                suggested += 1
        check()

    # Whatever is still unassigned gets grouped among itself: the loose
    # faces, in the order they were read, less the ones just given to people.
    taken = {face_id for face_id, *_ in assigned}
    candidates = []
    for row in loose:
        if int(row["id"]) in taken:
            continue
        vector = faces_mod.unpack(row["embedding"], dim)
        if vector is not None:
            candidates.append(facematch.Candidate(
                face_id=int(row["id"]), vector=vector,
                quality=float(row["quality"] or 0),
                asset_id=int(row["asset_id"])))

    clusters = facematch.cluster_faces(candidates, centroid_min=rules.cluster,
                                       link_min=rules.link, should_stop=should_stop)
    check()
    pairs: list[tuple[int, str | None]] = []
    hide: list[int] = []
    kept = 0
    for index, cluster in enumerate(clusters):
        if cluster.size < facematch.MIN_CLUSTER_SIZE:
            for face_id in cluster.members:
                pairs.append((face_id, None))
            continue
        # Hidden faces are grouped like any other, so that a group made
        # mostly of them is the same stranger again: the new faces in it are
        # hidden too, rather than coming back as a fresh group to wave away.
        hidden_here = sum(1 for face_id in cluster.members if face_id in hidden)
        stranger = bool(hidden_here) and hidden_here * 2 >= cluster.size
        if stranger:
            hide.extend(face_id for face_id in cluster.members if face_id not in hidden)
        # Unique rather than timestamped: a key made from the clock
        # second was the same for the same index in two regroups a moment
        # apart, and a key the console still has on screen must never come
        # to mean a different group.
        key = f"c{index:04d}-{uuid.uuid4().hex[:12]}"
        kept += 0 if stranger else 1
        for face_id in cluster.members:
            pairs.append((face_id, key))
    return {"assigned": assigned, "suggested": suggested, "pairs": pairs,
            "hide": hide, "clusters": kept, "loose_faces": len(candidates),
            "people": [person.person_id for person in people]}
