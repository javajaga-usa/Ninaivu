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


class FaceIndexer:
    """Detection and grouping for one library."""

    def __init__(self, cfg: Any, engine: faces_mod.FaceEngine | None = None) -> None:
        self.cfg = cfg
        self.engine = engine or faces_mod.FaceEngine(cfg.state_dir)
        self.crops_dir = Path(cfg.state_dir) / "faces"

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
        if not pending:
            return {"ok": True, "scanned": 0, "faces": 0, "failed": 0, "remaining": 0}

        def _load_image(p: Path, rot: int):
            if not p.exists():
                return None
            return faces_mod.load_image_for_faces(p, rot)

        workers = min(4, max(2, (os.cpu_count() or 4) // 2))
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
                    detected = self.engine.detect(image) if image is not None else None
                except Exception as exc:  # noqa: BLE001
                    log.debug("face detection failed on %s: %s", row.get("rel_path"), exc)
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
                "failed": failed, "remaining": max(0, total - scanned)}

    def _detect_one(self, path: Path, rotation: int):
        if not path.exists():
            return None
        image = faces_mod.load_image_for_faces(path, rotation)
        if image is None:
            return None
        return self.engine.detect(image)

    def _store(self, conn, asset_id: int, detected: Sequence) -> int:
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
    def regroup(self, conn, roots: Sequence[str] | str) -> dict[str, Any]:
        """Re-cluster loose faces and extend named people onto new ones.

        Order matters. Named people are matched *first*, so a face that
        clearly belongs to somebody already named never ends up founding an
        anonymous cluster the admin then has to merge by hand.
        """
        with _REGROUP_LOCK:
            return self._regroup(conn, roots)

    def _regroup(self, conn, roots: Sequence[str] | str) -> dict[str, Any]:
        people = self._load_people(conn)
        auto = suggested = 0

        loose = db.load_faces(conn, unassigned=True, roots=roots,
                              min_quality=CLUSTER_MIN_QUALITY)
        if people:
            # Every rejection at once rather than a query per loose face.
            blocked_for: dict[int, list[int]] = {}
            for face_id, person_id in db.rejections(conn):
                blocked_for.setdefault(face_id, []).append(person_id)
            assigned = []
            for row in loose:
                vector = faces_mod.unpack(row["embedding"])
                if vector is None:
                    continue
                result = facematch.assign_face(
                    vector, people, blocked=blocked_for.get(int(row["id"]), ()))
                if result.decision == "auto" and result.person_id:
                    assigned.append((int(row["id"]), result.person_id,
                                     "auto", result.score))
                elif result.decision == "suggest":
                    suggested += 1
            db.set_faces_person(conn, assigned)
            auto = len(assigned)

        # Whatever is still unassigned gets grouped among itself.
        remaining = db.load_faces(conn, unassigned=True, roots=roots,
                                  min_quality=CLUSTER_MIN_QUALITY)
        candidates = []
        for row in remaining:
            vector = faces_mod.unpack(row["embedding"])
            if vector is not None:
                candidates.append(facematch.Candidate(
                    face_id=int(row["id"]), vector=vector,
                    quality=float(row["quality"] or 0),
                    asset_id=int(row["asset_id"])))

        clusters = facematch.cluster_faces(candidates)
        pairs: list[tuple[int, str | None]] = []
        kept = 0
        for index, cluster in enumerate(clusters):
            if cluster.size < facematch.MIN_CLUSTER_SIZE:
                for face_id in cluster.members:
                    pairs.append((face_id, None))
                continue
            # Unique rather than timestamped: a key made from the clock
            # second was the same for the same index in two regroups a moment
            # apart, and a key the console still has on screen must never come
            # to mean a different group.
            key = f"c{index:04d}-{uuid.uuid4().hex[:12]}"
            kept += 1
            for face_id in cluster.members:
                pairs.append((face_id, key))
        db.save_cluster_keys(conn, pairs)

        for person in people:
            self.refresh_person(conn, person.person_id)

        return {"auto_assigned": auto, "suggested": suggested,
                "clusters": kept, "loose_faces": len(candidates)}

    # -- people -----------------------------------------------------------
    def _load_people(self, conn) -> list[facematch.Person]:
        """Named people, described only by faces a human confirmed.

        Automatic assignments are deliberately excluded from the centroid. If
        they fed back in, one wrong guess would shift the person's centre
        toward the wrong face, which makes the next wrong guess more likely —
        the failure mode where a person's album slowly fills with a stranger
        and every step looked reasonable.
        """
        people: list[facematch.Person] = []
        for row in db.list_people_clusters(conn):
            person_id = int(row["id"])
            confirmed = db.load_faces(conn, person_id=person_id)
            vectors = [v for v in
                       (faces_mod.unpack(f["embedding"]) for f in confirmed
                        if f["source"] == "confirmed")
                       if v is not None]
            if not vectors:
                continue
            centre = facematch.centroid(vectors)
            if centre is None:
                continue
            exemplars = self._exemplars(vectors)
            people.append(facematch.Person(person_id, centre, exemplars,
                                           row["name"]))
        return people

    @staticmethod
    def _exemplars(vectors: Sequence) -> list:
        chosen: list = []
        for vector in vectors:
            facematch._update_exemplars(chosen, facematch.unit(vector))
        return chosen

    def refresh_person(self, conn, person_id: int) -> None:
        """Rebuild one person's centroid from their confirmed faces."""
        faces = db.load_faces(conn, person_id=person_id)
        confirmed = [f for f in faces if f["source"] == "confirmed"]
        vectors = [v for v in (faces_mod.unpack(f["embedding"]) for f in confirmed)
                   if v is not None]
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
        people = {p.person_id: p for p in self._load_people(conn)}
        person = people.get(int(person_id))
        if person is None:
            return []
        loose = db.load_faces(conn, unassigned=True, roots=roots,
                              min_quality=CLUSTER_MIN_QUALITY)
        candidates = []
        for row in loose:
            vector = faces_mod.unpack(row["embedding"])
            if vector is not None:
                candidates.append(facematch.Candidate(
                    face_id=int(row["id"]), vector=vector,
                    quality=float(row["quality"] or 0),
                    asset_id=int(row["asset_id"])))
        blocked = db.rejections(conn, person_id=int(person_id))
        ranked = facematch.rank_for_person(person, candidates, blocked=blocked)

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
        stats["models"] = faces_mod.model_status(self.cfg.state_dir)
        stats["thresholds"] = {
            "auto": facematch.T_AUTO,
            "suggest": facematch.T_SUGGEST,
            "margin": facematch.MARGIN,
            "cluster": facematch.T_CLUSTER,
            "link": facematch.T_LINK,
        }
        return stats
