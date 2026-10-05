"""Regressions for the storage-layer review fixes.

Each test names the defect it pins: a rescan that undid an admin's tags and
content flag, a regroup that overwrote a face somebody had just confirmed, a
search index left empty by an interrupted rebuild, a paused full rescan that
came back as a quick one, and the smaller ones around it.
"""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.storage import db


VIEWER = 1


@pytest.fixture()
def conn(tmp_path):
    from ninaivu.server import auth

    connection = db.init_db(tmp_path / "t.db")
    auth.init_auth_schema(connection)
    auth.create_user(connection, "viewer1", "passwordone1", role="family")
    return connection


def open_as_the_app_does(path):
    """The database with its per-user tables, as every real run has it: reading
    an asset joins them, so a bare ``init_db`` is not enough to read one back."""
    from ninaivu.server import auth

    connection = db.init_db(path)
    auth.init_auth_schema(connection)
    return connection


def record(rel_path, **extra):
    """What the scanner's build_record hands the upsert: no AI verdicts."""
    base = {
        "root": "/lib",
        "rel_path": rel_path,
        "filename": rel_path.split("/")[-1],
        "folder": "/".join(rel_path.split("/")[:-1]),
        "ext": "jpg",
        "kind": "picture",
        "size": 1000,
        "mtime": 1.0,
        "captured_at": 1_600_000_000.0,
        "date_key": "2020-09-13",
        "width": 100,
        "height": 80,
        "tags": [],
        "ai_version": 0,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. A rescan keeps tags, captions and the content flag
# ---------------------------------------------------------------------------

def test_a_rescan_keeps_what_an_admin_set_by_hand(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    # The path POST /api/asset/<id> saves by.
    db.update_asset(conn, asset_id, nsfw=1, tags=["mine"], caption="Grandma")

    db.upsert_asset(conn, record("a/1.jpg", size=2000))
    db.bulk_upsert(conn, [record("a/1.jpg", size=3000)])

    row = db.get_asset(conn, asset_id)
    assert row["size"] == 3000
    assert row["nsfw"] is True, "a rescan must not unflag explicit content"
    assert row["tags"] == ["mine"]
    assert row["caption"] == "Grandma"
    assert row["nsfw_source"] == row["tags_source"] == row["caption_source"] == "manual"


def test_a_rescan_keeps_the_ai_verdicts_until_the_retag_replaces_them(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    db.store_ai_fields(conn, asset_id, ai_version=2, tags=["dog"],
                       caption="a dog", nsfw=1, nsfw_score=0.9)

    # A plain re-index: no verdict, empty tags, no caption.
    db.bulk_upsert(conn, [record("a/1.jpg", size=2000)])

    row = db.get_asset(conn, asset_id)
    assert row["nsfw"] is True and row["nsfw_score"] == pytest.approx(0.9)
    assert row["tags"] == ["dog"]
    assert row["caption"] == "a dog"
    assert row["tags_source"] == "auto"


def test_a_value_the_probe_really_read_still_replaces_an_automatic_one(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    db.store_ai_fields(conn, asset_id, tags=["dog"], caption="a dog",
                       nsfw=1, nsfw_score=0.9)

    db.upsert_asset(conn, record("a/1.jpg", tags=["album"], caption="From EXIF",
                                 nsfw=0, nsfw_score=0.1))

    row = db.get_asset(conn, asset_id)
    assert row["tags"] == ["album"]
    assert row["caption"] == "From EXIF"
    assert row["nsfw"] is False


def test_the_ai_pass_leaves_an_admins_values_alone(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    db.update_asset(conn, asset_id, tags=["mine"], nsfw=0)

    db.store_ai_fields(conn, asset_id, ai_version=7, tags=["ai"],
                       caption="ai caption", nsfw=1, nsfw_score=0.99)

    row = db.get_asset(conn, asset_id)
    assert row["ai_version"] == 7, "the pass still records that it ran"
    assert row["tags"] == ["mine"]
    assert row["nsfw"] is False
    # Nobody set the caption by hand, so the model's is taken.
    assert row["caption"] == "ai caption"


def test_tag_batch_keeps_manual_values_after_a_model_change(cfg, tmp_path):
    from ninaivu.media.scanner import AI_VERSION, Scanner

    connection = open_as_the_app_does(cfg.db_path)
    asset_id = db.upsert_asset(connection, record("a/1.jpg"))
    db.update_asset(connection, asset_id, tags=["mine"], caption="Grandma", nsfw=1)
    # What _retag_if_the_model_changed does to every row.
    connection.execute("UPDATE assets SET ai_version = 0")
    connection.commit()

    class Stub:
        name = "stub"
        model_id = "stub-v1"

        def analyse(self, paths, **_):
            return [{"tags": ["ai"], "caption": "ai caption", "nsfw_score": 0.0}
                    for _ in paths]

    cfg.nsfw_filter = True
    scanner = Scanner(cfg)
    scanner.ai = Stub()
    scanner._tag_batch(connection, [(asset_id, tmp_path / "x.jpg")])

    row = db.get_asset(connection, asset_id)
    assert row["ai_version"] == AI_VERSION
    assert row["tags"] == ["mine"]
    assert row["caption"] == "Grandma"
    assert row["nsfw"] is True


def test_the_source_columns_reach_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    connection = db.init_db(path)
    for column in ("tags_source", "caption_source", "nsfw_source"):
        assert column in db.LATE_COLUMNS["assets"]
        try:
            connection.execute(f"ALTER TABLE assets DROP COLUMN {column}")
        except Exception:                       # noqa: BLE001 - SQLite < 3.35
            pytest.skip("this SQLite cannot drop a column to fake an old file")
    connection.commit()
    db.close_all()
    reopened = db.init_db(path)
    assert {"tags_source", "caption_source", "nsfw_source"} <= db.columns(reopened, "assets")


# ---------------------------------------------------------------------------
# 2. Regrouping faces
# ---------------------------------------------------------------------------

def _face(connection, asset_id, **extra):
    cur = connection.execute(
        "INSERT INTO faces(asset_id, bbox, embedding, person_id, source) "
        "VALUES (?, '[0,0,1,1]', x'00', ?, ?)",
        (asset_id, extra.get("person_id"), extra.get("source", "none")))
    connection.commit()
    return int(cur.lastrowid)


def test_an_automatic_guess_never_overwrites_a_confirmed_face(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    anna = db.create_or_update_person_cluster(conn, "Anna")["id"]
    ben = db.create_or_update_person_cluster(conn, "Ben")["id"]
    face = _face(conn, asset_id, person_id=anna, source="confirmed")

    db.set_faces_person(conn, [(face, ben, "auto", 0.9)])

    row = db.get_face(conn, face)
    assert row["person_id"] == anna and row["source"] == "confirmed"


def test_an_automatic_guess_respects_a_rejection_made_meanwhile(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    ben = db.create_or_update_person_cluster(conn, "Ben")["id"]
    face = _face(conn, asset_id)
    db.reject_face_for_person(conn, face, ben)

    db.set_faces_person(conn, [(face, ben, "auto", 0.9)])

    assert db.get_face(conn, face)["person_id"] is None


def test_an_automatic_guess_still_lands_on_a_loose_face(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    ben = db.create_or_update_person_cluster(conn, "Ben")["id"]
    face = _face(conn, asset_id)

    db.set_faces_person(conn, [(face, ben, "auto", 0.9)])

    row = db.get_face(conn, face)
    assert row["person_id"] == ben and row["source"] == "auto"


def test_a_human_confirmation_always_lands(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    anna = db.create_or_update_person_cluster(conn, "Anna")["id"]
    ben = db.create_or_update_person_cluster(conn, "Ben")["id"]
    face = _face(conn, asset_id, person_id=anna, source="auto")

    db.set_faces_person(conn, [(face, ben, "confirmed", 1.0)])

    row = db.get_face(conn, face)
    assert row["person_id"] == ben and row["source"] == "confirmed"


def test_two_regroups_in_the_same_second_never_share_a_cluster_key(monkeypatch):
    from ninaivu.media import faceindex, facematch

    rows = [{"id": i, "embedding": b"x", "quality": 1.0, "asset_id": i}
            for i in range(1, 4)]
    monkeypatch.setattr(faceindex.db, "load_faces", lambda *a, **k: rows)
    monkeypatch.setattr(faceindex.faces_mod, "unpack", lambda blob: [1.0])
    monkeypatch.setattr(faceindex.facematch, "cluster_faces", lambda c: [
        SimpleNamespace(size=facematch.MIN_CLUSTER_SIZE, members=[1, 2, 3])])
    saved = []
    monkeypatch.setattr(faceindex.db, "save_cluster_keys",
                        lambda conn, pairs: saved.append(pairs))

    indexer = faceindex.FaceIndexer.__new__(faceindex.FaceIndexer)
    monkeypatch.setattr(indexer, "_load_people", lambda conn: [])
    indexer.regroup(None, ["/lib"])
    indexer.regroup(None, ["/lib"])

    first = {key for _, key in saved[0]}
    second = {key for _, key in saved[1]}
    assert len(first) == len(second) == 1
    assert first != second


def test_only_one_regroup_runs_at_a_time(monkeypatch):
    from ninaivu.media import faceindex

    inside = []

    def slow(self, conn, roots):
        inside.append(faceindex._REGROUP_LOCK.locked())
        return {}

    monkeypatch.setattr(faceindex.FaceIndexer, "_regroup", slow)
    indexer = faceindex.FaceIndexer.__new__(faceindex.FaceIndexer)
    indexer.regroup(None, ["/lib"])
    assert inside == [True]
    assert not faceindex._REGROUP_LOCK.locked()


# ---------------------------------------------------------------------------
# 3. An interrupted full-text rebuild is finished at the next start
# ---------------------------------------------------------------------------

def test_an_interrupted_fts_rebuild_is_finished_on_the_next_start(tmp_path):
    path = tmp_path / "fts.db"
    connection = db.init_db(path)
    if not db.has_fts5(connection):
        pytest.skip("this SQLite has no FTS5")
    db.upsert_asset(connection, record("a/lighthouse.jpg", tags=["lighthouse"]))

    # What _heal_fts leaves behind when the process dies before the rebuild.
    for trigger in ("assets_ai", "assets_ad", "assets_au"):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    connection.execute("DROP TABLE assets_fts")
    db.set_meta(connection, db.FTS_REBUILD_KEY, "1")
    connection.commit()
    db.close_all()

    reopened = open_as_the_app_does(path)
    items, total = db.query_assets(reopened, "/lib", text="lighthouse", limit=9)
    assert total == 1, "search must not stay empty after an interrupted rebuild"
    assert db.get_meta(reopened, db.FTS_REBUILD_KEY) == "0"


# ---------------------------------------------------------------------------
# 4/5. The indexer stands down and comes back as the same scan
# ---------------------------------------------------------------------------

def test_a_paused_full_rescan_resumes_as_a_full_rescan(cfg):
    from ninaivu.media.scanner import Scanner

    scanner = Scanner(cfg)
    release = threading.Event()
    fake = threading.Thread(target=release.wait, daemon=True)
    fake.start()
    root = Path(cfg.active_root)
    try:
        scanner._thread = fake
        scanner._current = ([root], True)
        assert scanner.defer("a consolidation is running") is True
        assert scanner._defer_pending == ([root], True)
    finally:
        release.set()
        fake.join(timeout=5)
        with scanner._lock:
            scanner._claims.clear()
            scanner._defer_pending = None
        scanner.stop(join=True)


def test_a_scan_asked_for_while_held_is_queued_with_its_full_flag(cfg):
    from ninaivu.media.scanner import Scanner

    scanner = Scanner(cfg)
    scanner.defer("a consolidation is running")
    scanner.start(full=True)
    assert not scanner.running
    assert scanner._defer_pending is not None and scanner._defer_pending[1] is True
    with scanner._lock:
        scanner._claims.clear()
        scanner._defer_pending = None
    scanner.stop(join=True)


# ---------------------------------------------------------------------------
# 6/11. Config files are replaced whole and synced
# ---------------------------------------------------------------------------

def test_config_save_syncs_before_replacing(tmp_path, monkeypatch):
    from ninaivu.server import config as config_mod

    cfg = config_mod.Config()
    cfg.state_dir = tmp_path / "state"
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(config_mod.os, "fsync",
                        lambda fd: synced.append(fd) or real_fsync(fd))
    cfg.save()

    assert synced, "the file must reach the disk before the rename"
    assert json.loads(cfg.config_path.read_text(encoding="utf-8")) is not None
    assert not cfg.config_path.with_suffix(".json.tmp").exists()


def test_reroot_rewrites_config_by_replacing_it(tmp_path, monkeypatch):
    from ninaivu.storage import reroot

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"roots": ["E:/Photos"], "active_root": "E:/Photos",
                                "smtp_password": "x"}), encoding="utf-8")
    old = reroot.normalise("E:/Photos")
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(reroot.os, "replace",
                        lambda a, b: replaced.append((a, b)) or real_replace(a, b))

    assert reroot.rewrite_config(path, old, "D:/Photos", dry_run=False) is True

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["roots"] == ["D:/Photos"] and stored["active_root"] == "D:/Photos"
    assert stored["smtp_password"] == "x"
    assert replaced and Path(replaced[0][1]) == path
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 7. The archive lets go of a claim when the copy cannot even start
# ---------------------------------------------------------------------------

@pytest.fixture()
def work(tmp_path):
    from ninaivu import archive
    from ninaivu.archive import database as archive_db

    archive.configure(tmp_path)
    archive_db.close_db()
    archive_db.init_db()
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        yield str(tree)
    finally:
        archive_db.close_db()


def test_a_claim_is_released_when_the_folder_cannot_be_made(work, monkeypatch):
    from ninaivu.archive import database as archive_db
    from ninaivu.archive import scanner as archive_scanner

    src = os.path.join(work, "s")
    os.makedirs(src)
    photo = os.path.join(src, "a.jpg")
    with open(photo, "wb") as f:
        f.write(b"\xff\xd8" + b"x" * 500)
    job = archive_scanner.ArchiveJob([src], os.path.join(work, "d"))
    job.job_id = archive_db.create_job([src], job.destination, "date")

    # Hashed before the copy, so the bytes are claimed first...
    monkeypatch.setattr(archive_scanner.db, "size_is_known", lambda size, **scope: True)

    # ...and then the destination folder cannot be recorded or made.
    def broken(job_id, folder):
        raise OSError("the destination drive went away")
    monkeypatch.setattr(archive_scanner.db, "note_job_folder", broken)

    with pytest.raises(OSError):
        job.process_file(photo, "a.jpg")
    assert job._inflight == {}, "the claim must not outlive the failed attempt"


# ---------------------------------------------------------------------------
# 8. Stable paging for rating and random orders
# ---------------------------------------------------------------------------

def _pages(connection, sort, **extra):
    seen = []
    for offset in range(0, 6, 2):
        items, _ = db.query_assets(connection, "/lib", sort=sort, limit=2,
                                   offset=offset, **extra)
        seen += [item["id"] for item in items]
    return seen


def test_rating_order_pages_without_repeats(conn):
    for i in range(6):
        db.upsert_asset(conn, record(f"a/{i}.jpg"))
    seen = _pages(conn, "rating_desc")
    assert len(seen) == len(set(seen)) == 6


def test_random_order_is_stable_across_pages(conn):
    for i in range(6):
        db.upsert_asset(conn, record(f"a/{i}.jpg"))
    seen = _pages(conn, "random", seed=42)
    assert len(seen) == len(set(seen)) == 6
    assert _pages(conn, "random", seed=42) == seen
    whole, _ = db.query_assets(conn, "/lib", sort="random", seed=42, limit=9)
    assert [item["id"] for item in whole] == seen


# ---------------------------------------------------------------------------
# 9. LIKE wildcards in what people type are literal
# ---------------------------------------------------------------------------

def test_an_underscore_in_a_tag_is_not_a_wildcard(conn):
    db.upsert_asset(conn, record("a/1.jpg", tags=["a_b"]))
    db.upsert_asset(conn, record("a/2.jpg", tags=["axb"]))
    assert db.query_assets(conn, "/lib", tag="a_b", limit=9)[1] == 1


def test_a_percent_in_the_search_box_is_not_a_wildcard(conn):
    db.set_meta(conn, "fts", "0")          # the LIKE fallback
    conn.commit()
    db.upsert_asset(conn, record("a/100%.jpg"))
    db.upsert_asset(conn, record("a/1000.jpg"))
    items, total = db.query_assets(conn, "/lib", text="100%", limit=9)
    assert total == 1 and items[0]["filename"] == "100%.jpg"


# ---------------------------------------------------------------------------
# 10. Flagged content is not counted for anybody below an admin
# ---------------------------------------------------------------------------

def test_stats_and_facets_leave_flagged_content_out_below_admin(conn):
    db.upsert_asset(conn, record("a/1.jpg", camera="Cam"))
    db.upsert_asset(conn, record("b/2.jpg", camera="Secret", nsfw=1, nsfw_score=0.9))

    family = db.library_stats(conn, "/lib", viewer_id=VIEWER, max_visibility=1)
    assert family["count"] == 1 and family["nsfw"] == 0
    admin = db.library_stats(conn, "/lib", viewer_id=VIEWER, max_visibility=2)
    assert admin["count"] == 2 and admin["nsfw"] == 1

    facets = db.facets(conn, "/lib", max_visibility=1)
    assert [c["name"] for c in facets["cameras"]] == ["Cam"]
    assert "b" not in [f["name"] for f in facets["folders"]]
    assert "Secret" in [c["name"] for c in db.facets(conn, "/lib", max_visibility=2)["cameras"]]
