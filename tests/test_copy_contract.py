"""One copy contract (ninaivu/copies/, 4.0 proposal 4): the adapters, the
ledger (index migration 3), the headline and the console's read."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu import copies
from ninaivu.cloud import store
from ninaivu.cloud.offsite import SCHEMA as OFFSITE_SCHEMA
from ninaivu.copies import ledger
from ninaivu.mugil import courier
from ninaivu.storage import db, fingerprints, migrations
from ninaivu.storage.mirror import SCHEMA as MIRROR_SCHEMA


def _engine_tables(conn) -> dict[str, list[tuple]]:
    """Every row each engine records, to show filling the ledger changed none."""
    out = {}
    for table in ("mirror_copies", "mirror_meta", "offsite_copies", "offsite_meta",
                  "cloud_uploads"):
        out[table] = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
    return out


@pytest.fixture()
def recorded(scanned):
    """A library whose four copies each recorded some of its files, in the
    shapes the engines leave them, and the ledger not yet filled."""
    cfg, conn, _ = scanned
    for root in cfg.roots:
        fingerprints.fill(conn, root)
    conn.executescript(MIRROR_SCHEMA)
    conn.executescript(OFFSITE_SCHEMA)
    store.init_schema(conn)
    assets = [dict(r) for r in conn.execute(
        "SELECT a.id, a.root, a.rel_path, a.size, a.mtime, f.digest, f.algo, "
        "f.algo || ':' || f.digest AS fp FROM assets a "
        "JOIN content_fingerprints f ON f.asset_id = a.id WHERE a.trashed = 0 ORDER BY a.id")]
    assert len(assets) >= 6
    a, b, c, d, e, stale = assets[:6]
    now = time.time()
    for row in (a, b):
        conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime, sha256, copied_at) "
                     "VALUES (?,?,?,?,?,?)", (row["root"], row["rel_path"], row["size"],
                                             row["mtime"], "x", now - 100))
    # Copied when the file was smaller: says nothing about it as it is now.
    conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime, sha256, copied_at) "
                 "VALUES (?,?,?,?,?,?)", (stale["root"], stale["rel_path"], stale["size"] - 1,
                                         stale["mtime"], "x", now - 100))
    conn.execute("INSERT INTO offsite_copies(root, rel_path, size, mtime, object, uploaded_at) "
                 "VALUES (?,?,?,?,?,?)", (b["root"], b["rel_path"], b["size"], b["mtime"],
                                         "obj", now - 50))
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, size, state, done_at) "
                 "VALUES (?,?,?,?,?)", (c["root"], c["rel_path"], c["size"], "done", now - 20))
    conn.execute("INSERT INTO cloud_uploads(root, rel_path, size, state) VALUES (?,?,?,?)",
                 (e["root"], e["rel_path"], e["size"], "pending"))
    conn.commit()
    with courier.sent_record(cfg.state_dir) as sent:
        sent.execute("INSERT INTO sent VALUES (?,?,?,?,?,?,?)",
                     (d["root"], d["rel_path"], d["size"], d["mtime"], d["algo"], d["digest"],
                      now - 10))
        # The Secondary confirmed different bytes than the index has now.
        sent.execute("INSERT INTO sent VALUES (?,?,?,?,?,?,?)",
                     (e["root"], e["rel_path"], e["size"], e["mtime"], e["algo"], "0" * 64,
                      now - 10))
    return SimpleNamespace(cfg=cfg, conn=conn, a=a, b=b, c=c, d=d, e=e, stale=stale,
                           files=len(assets))


def _reopen_as_37(cfg, conn):
    """The index as 3.7 left it: no ledger, no migration recorded. Then opened."""
    conn.execute("DROP TABLE IF EXISTS copies")
    conn.commit()
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    db.close(cfg.db_path)
    db._READY.discard(str(cfg.db_path))                          # noqa: SLF001
    return db.init_db(cfg.db_path)


def _ledger(conn) -> dict[tuple[str, str], str]:
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT fingerprint, destination, state FROM copies")}


# -- the migrations --------------------------------------------------------------

def test_two_is_the_locations_table_and_three_is_the_ledger():
    names = [m.name for m in migrations.INDEX]
    assert names[1] == "where each photograph is"
    assert names[2] == "the copies ledger" and migrations.latest(migrations.INDEX) >= 3


def test_a_new_index_has_the_ledger(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    try:
        assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(copies)")]
        assert columns == ["fingerprint", "destination", "state", "verified_at", "size"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(copies)") if r[5]] == [
            "fingerprint", "destination"]
    finally:
        db.close(tmp_path / "index.db")


def test_a_37_index_is_filled_from_what_the_engines_recorded(recorded):
    cfg, conn = recorded.cfg, recorded.conn
    before = _engine_tables(conn)
    with courier.sent_record(cfg.state_dir) as sent:
        sent_before = [tuple(r) for r in sent.execute("SELECT * FROM sent ORDER BY 1, 2")]
    conn = _reopen_as_37(cfg, conn)
    assert migrations.recorded(conn) == migrations.latest(migrations.INDEX)
    got = _ledger(conn)
    r = recorded
    assert got == {
        (r.a["fp"], "mirror"): "kept", (r.b["fp"], "mirror"): "kept",
        (r.b["fp"], "offsite"): "kept", (r.c["fp"], "drive"): "kept",
        (r.d["fp"], "secondary"): "kept",
    }
    # Nothing is re-sent and no engine is told anything: their records are
    # exactly as they were, the Secondary's too.
    assert _engine_tables(conn) == before
    with courier.sent_record(cfg.state_dir) as sent:
        assert [tuple(r) for r in sent.execute("SELECT * FROM sent ORDER BY 1, 2")] == sent_before
    size, when = conn.execute("SELECT size, verified_at FROM copies WHERE destination='drive'"
                              ).fetchone()
    assert size == r.c["size"] and when > 0


def test_opening_again_changes_nothing(recorded):
    conn = _reopen_as_37(recorded.cfg, recorded.conn)
    first = conn.execute("SELECT * FROM copies ORDER BY 1, 2").fetchall()
    db.close(recorded.cfg.db_path)
    db._READY.discard(str(recorded.cfg.db_path))                 # noqa: SLF001
    conn = db.init_db(recorded.cfg.db_path)
    assert [tuple(r) for r in conn.execute("SELECT * FROM copies ORDER BY 1, 2")] == [
        tuple(r) for r in first]


def test_the_migration_only_adds(recorded):
    """Go back to 3.7: everything 3.7 had is there as it was; only new tables."""
    conn = recorded.conn
    conn.execute("DROP TABLE IF EXISTS copies")
    conn.commit()
    before = {tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master")}
    conn = _reopen_as_37(recorded.cfg, conn)
    after = {tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master")}
    assert before <= after
    assert {name for _type, name, _sql in after - before} == {"copies", "idx_copies_destination"}


# -- filling it again ------------------------------------------------------------

def test_a_copy_whose_record_went_is_unconfirmed_not_forgotten(recorded):
    cfg, conn = recorded.cfg, recorded.conn
    ledger.refresh(conn, state_dir=cfg.state_dir)
    a = recorded.a
    conn.execute("DELETE FROM mirror_copies WHERE root=? AND rel_path=?",
                 (a["root"], a["rel_path"]))
    conn.commit()
    kept = ledger.refresh(conn, state_dir=cfg.state_dir)
    assert kept["mirror"] == 1
    assert _ledger(conn)[(a["fp"], "mirror")] == "unconfirmed"
    # And kept again when the record says so again.
    conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime, copied_at) "
                 "VALUES (?,?,?,?,?)", (a["root"], a["rel_path"], a["size"], a["mtime"], 1.0))
    conn.commit()
    ledger.refresh(conn, state_dir=cfg.state_dir)
    assert _ledger(conn)[(a["fp"], "mirror")] == "kept"


def test_offsite_sent_somewhere_else_counts_for_nothing_here(recorded):
    conn = recorded.conn
    conn.execute("INSERT INTO offsite_meta(key, value) VALUES ('dest_where', 'old place')")
    conn.commit()
    assert ledger.refresh(conn, offsite_where="new place")["offsite"] == 0
    assert ledger.refresh(conn, offsite_where="old place")["offsite"] == 1


def test_an_unreadable_secondary_record_leaves_its_rows_alone(recorded, monkeypatch):
    cfg, conn = recorded.cfg, recorded.conn
    ledger.refresh(conn, state_dir=cfg.state_dir)
    monkeypatch.setitem(ledger.SOURCES, "secondary", lambda c, **_: False)
    kept = ledger.refresh(conn, state_dir=cfg.state_dir)
    assert "secondary" not in kept
    assert _ledger(conn)[(recorded.d["fp"], "secondary")] == "kept"


# -- the headline ----------------------------------------------------------------

def test_the_headline_counts_photographs_in_two_places(recorded):
    cfg, conn = recorded.cfg, recorded.conn
    ledger.refresh(conn, state_dir=cfg.state_dir)
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT f.digest) FROM assets a JOIN content_fingerprints f "
        "ON f.asset_id = a.id WHERE a.trashed = 0").fetchone()[0]
    got = ledger.headline(conn, cfg.roots)
    # a, b, c and d are somewhere else; b twice, still one photograph.
    assert got == {"two_places": 4, "total": distinct, "unfingerprinted": 0}


def test_a_file_without_a_fingerprint_is_never_in_two_places(recorded):
    cfg, conn = recorded.cfg, recorded.conn
    ledger.refresh(conn, state_dir=cfg.state_dir)
    conn.execute("DELETE FROM content_fingerprints WHERE asset_id = ?", (recorded.a["id"],))
    conn.commit()
    got = ledger.headline(conn, cfg.roots)
    assert got["unfingerprinted"] == 1 and got["two_places"] == 3


# -- the adapters ----------------------------------------------------------------

def test_every_engine_has_an_adapter_that_keeps_the_contract(app):
    services = app.config["MV_SERVICES"]
    dests = copies.destinations(services)
    assert [d.name for d in dests] == ["mirror", "secondary", "offsite", "drive"]
    shapes = set()
    for dest in dests:
        assert isinstance(dest, copies.Destination)
        assert dest.kind in copies.KINDS
        assert dest.keeps and dest.keeps <= set(copies.KEEPS)
        assert copies.ORIGINALS in dest.keeps
        status = dest.status()
        shapes.add(tuple(sorted(status)))
        assert status["name"] == dest.name and status["on"] is False
        assert isinstance(dest.schedule, dict) and "every_hours" in dest.schedule
    assert len(shapes) == 1                       # one shape for all of them


def test_what_leaves_the_house_encrypts(app):
    cfg = app.config["MV_SERVICES"].cfg
    by_name = {d.name: d for d in copies.destinations(app.config["MV_SERVICES"])}
    offsite, drive = by_name["offsite"], by_name["drive"]
    assert offsite.encrypts is True
    cfg.offsite_kind = "s3"
    assert offsite.kind == "s3" and copies.problems(offsite) == []
    cfg.cloud_encrypt = True
    assert drive.encrypts and copies.problems(drive) == []
    cfg.cloud_encrypt = False
    assert drive.encrypts is False
    assert any("unencrypted" in p for p in copies.problems(drive))
    assert any("unencrypted" in p for p in drive.status()["problems"])
    # A copy at home does not have to.
    assert copies.problems(by_name["mirror"]) == []


def test_what_each_keeps_follows_its_settings(app):
    services = app.config["MV_SERVICES"]
    by_name = {d.name: d for d in copies.destinations(services)}
    services.cfg.mugil_family_copy = True
    assert copies.FAMILY_VIEW in by_name["secondary"].keeps
    services.cfg.cloud_index_every_hours = 24
    assert copies.INDEX in by_name["drive"].keeps
    services.cfg.cloud_index_every_hours = 0
    assert copies.INDEX not in by_name["drive"].keeps


def test_the_ledger_reads_go_through_each_adapter(recorded):
    cfg, conn = recorded.cfg, recorded.conn
    ledger.refresh(conn, state_dir=cfg.state_dir)
    mirror = copies.MirrorDestination(SimpleNamespace())
    assert mirror.ledger(conn)["kept"] == 2
    assert mirror.holds(conn, recorded.a["fp"])
    assert not mirror.holds(conn, recorded.c["fp"])


def test_the_restore_test_hooks(app):
    services = app.config["MV_SERVICES"]
    by_name = {d.name: d for d in copies.destinations(services)}
    # No folder chosen: the second disk says why it cannot check.
    mirror = by_name["mirror"].restore_test()
    assert mirror["started"] is False and mirror["reason"]
    assert by_name["secondary"].restore_test()["started"] is False
    offsite = by_name["offsite"].restore_test()
    assert offsite["started"] is False and offsite["reason"]

    class Probe:
        cfg = services.cfg

        def restore_sample(self, count):
            return {"ok": True, "checked": count, "files": []}

    assert copies.OffsiteDestination(Probe()).restore_test() == {
        "started": True, "ok": True, "checked": 3, "files": [],
        "test": "a sample restored and compared"}

    class Tests:
        def __init__(self):
            self.asked = 0

        def run_in_background(self):
            self.asked += 1
            return True

    tests = Tests()
    drive = copies.DriveDestination(services.cloud, restore_tests=tests)
    assert drive.restore_test()["started"] is True and tests.asked == 1


# -- the console's read ----------------------------------------------------------

def test_the_console_reads_every_copy_and_the_headline(app, as_admin, monkeypatch):
    services = app.config["MV_SERVICES"]
    cfg = services.cfg
    conn = db.connect(cfg.db_path)
    for root in cfg.roots:
        fingerprints.fill(conn, root)
    conn.executescript(MIRROR_SCHEMA)
    first = conn.execute("SELECT root, rel_path, size, mtime FROM assets LIMIT 1").fetchone()
    conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime, copied_at) "
                 "VALUES (?,?,?,?,?)", (*first, 1.0))
    conn.commit()
    monkeypatch.setattr(copies, "_filled", {})
    # Filled by the supervisor's job, not by the console's read.
    copies.fill_job(services).run()
    answer = as_admin.get("/api/admin/copies")
    assert answer.status_code == 200
    data = answer.get_json()
    assert [d["name"] for d in data["destinations"]] == ["mirror", "secondary", "offsite",
                                                          "drive"]
    mirror = data["destinations"][0]
    assert mirror["ledger"]["kept"] == 1 and mirror["kind"] == "folder"
    headline = data["headline"]
    assert headline["two_places"] == 1 and headline["total"] >= 6


def test_the_console_read_is_the_administrators(as_family, anon):
    assert as_family.get("/api/admin/copies").status_code in (401, 403)
    assert anon.get("/api/admin/copies").status_code in (401, 403)


def test_the_route_is_in_the_manifest():
    from pathlib import Path
    text = (Path(__file__).parent / "data" / "route_manifest.txt").read_text(encoding="utf-8")
    line = next(li for li in text.splitlines() if "| /api/admin/copies |" in li)
    assert "| admin |" in line and "| reads |" in line


def test_the_ledger_is_filled_at_most_every_few_minutes(recorded, monkeypatch):
    calls = []
    monkeypatch.setattr(ledger, "refresh", lambda conn, **kw: calls.append(kw) or {})
    monkeypatch.setattr(copies, "_filled", {})
    conn = recorded.conn
    assert copies.refresh(conn, [], recorded.cfg.state_dir, now=1000.0)
    assert not copies.refresh(conn, [], recorded.cfg.state_dir, now=1000.0 + 60)
    assert copies.refresh(conn, [], recorded.cfg.state_dir, now=1000.0 + copies.FILL_EVERY + 1)
    assert len(calls) == 2


def test_an_in_memory_index_has_no_state_folder():
    conn = sqlite3.connect(":memory:")
    assert ledger.state_dir_of(conn) is None


# --- Audit A-04, carried to the Secondary's hand-over ------------------------------

def test_a_family_setting_changed_alone_makes_the_secondary_hand_over_due(cfg, monkeypatch):
    """A setting the family app obeys (the content filter) writes nothing to the
    index; the hand-over must still go, or the Secondary keeps the old one."""
    import time as _time
    from ninaivu.mugil import handover
    monkeypatch.setattr(handover, "wanted", lambda _cfg: True)
    keeper = handover.Handover(cfg)
    keeper._remember(at=_time.time() - handover.EVERY - 5, stamp=keeper._stamp(), error="")
    assert keeper.due() is False                      # nothing changed
    cfg.nsfw_filter = not cfg.nsfw_filter
    assert keeper.due() is True


# --- the engines write the ledger as they confirm a copy (second slice) -------------

def test_the_console_read_writes_nothing(app, as_admin, monkeypatch):
    services = app.config["MV_SERVICES"]
    conn = db.connect(services.cfg.db_path)
    for root in services.cfg.roots:
        fingerprints.fill(conn, root)
    conn.executescript(MIRROR_SCHEMA)
    first = conn.execute("SELECT root, rel_path, size, mtime FROM assets LIMIT 1").fetchone()
    conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime, copied_at) "
                 "VALUES (?,?,?,?,?)", (*first, 1.0))
    conn.commit()
    monkeypatch.setattr(copies, "_filled", {})
    before = conn.total_changes
    rows = conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0]
    assert as_admin.get("/api/admin/copies").status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == rows
    assert conn.total_changes == before


def test_the_fill_is_a_supervisor_job(app):
    services = app.config["MV_SERVICES"]
    job = copies.fill_job(services)
    assert job.name == "copies-ledger" and job.trigger.kind == "every"
    assert job.trigger.interval() == copies.FILL_EVERY and "index_write" in job.claims
    assert services.supervisor.state("copies-ledger") is not None


def test_a_confirmed_copy_is_written_verified(recorded):
    conn, a = recorded.conn, recorded.a
    at = time.time()
    assert ledger.confirm(conn, "mirror", [(a["root"], a["rel_path"], a["size"], a["mtime"])],
                          at=at) == 1
    conn.commit()
    row = conn.execute("SELECT state, verified_at, size FROM copies WHERE fingerprint=? "
                       "AND destination='mirror'", (a["fp"],)).fetchone()
    assert tuple(row) == ("verified", at, a["size"])
    assert ledger.holds(conn, "mirror", a["fp"])
    assert ledger.counts(conn, "mirror")["verified"] == 1


def test_a_copy_of_other_bytes_is_not_confirmed(recorded):
    conn, a = recorded.conn, recorded.a
    # Another size, another time, or another fingerprint at the other end.
    assert ledger.confirm(conn, "mirror", [(a["root"], a["rel_path"], a["size"] + 1, None)],
                          at=1.0) == 0
    assert ledger.confirm(conn, "mirror", [(a["root"], a["rel_path"], a["size"],
                                            a["mtime"] + 5)], at=1.0) == 0
    assert ledger.confirm(conn, "secondary", [(a["root"], a["rel_path"], a["size"], None)],
                          at=1.0, expect=[f"{a['algo']}:{'0' * 64}"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0] == 0


def test_without_a_ledger_confirming_changes_nothing(tmp_path):
    conn = sqlite3.connect(":memory:")
    assert ledger.confirm(conn, "mirror", [("/x", "a.jpg", 1, None)], at=1.0) == 0


def test_the_fill_keeps_verified_and_marks_unconfirmed_when_the_record_goes(recorded):
    cfg, conn, a = recorded.cfg, recorded.conn, recorded.a
    ledger.confirm(conn, "mirror", [(a["root"], a["rel_path"], a["size"], a["mtime"])], at=5.0)
    conn.commit()
    ledger.refresh(conn, state_dir=cfg.state_dir)
    assert _ledger(conn)[(a["fp"], "mirror")] == "verified"
    assert _ledger(conn)[(recorded.b["fp"], "mirror")] == "kept"
    conn.execute("DELETE FROM mirror_copies WHERE root=? AND rel_path=?",
                 (a["root"], a["rel_path"]))
    conn.commit()
    ledger.refresh(conn, state_dir=cfg.state_dir)
    assert _ledger(conn)[(a["fp"], "mirror")] == "unconfirmed"


def test_drive_writes_the_ledger_when_an_upload_is_done(recorded):
    conn, e = recorded.conn, recorded.e
    store.record_done(conn, e["root"], e["rel_path"], remote_id="r1")
    assert _ledger(conn)[(e["fp"], "drive")] == "verified"


def test_the_secondary_confirmation_writes_the_ledger(recorded):
    cfg, conn, a, b = recorded.cfg, recorded.conn, recorded.a, recorded.b
    keeper = courier.Courier(cfg, lambda: conn)
    item = lambda r, digest: {"path": r["rel_path"], "size": r["size"], "mtime": r["mtime"],  # noqa: E731
                              "algo": r["algo"], "digest": digest}
    keeper._record(a["root"], [item(a, a["digest"]), item(b, "0" * 64)], conn)  # noqa: SLF001
    got = _ledger(conn)
    assert got[(a["fp"], "secondary")] == "verified"
    assert (b["fp"], "secondary") not in got


@pytest.fixture()
def second_disk(scanned, tmp_path):
    from ninaivu import build_services
    cfg, conn, _ = scanned
    cfg.watch = False
    for root in cfg.roots:
        fingerprints.fill(conn, root)
    services = build_services(cfg)
    services.scanner.stop()
    disk = tmp_path / "other-disk"
    disk.mkdir()
    cfg.mirror_dir, cfg.mirror_enabled = str(disk), True
    return cfg, conn, services.mirror


def test_the_second_disk_writes_the_ledger_as_it_copies(second_disk):
    cfg, conn, mirror = second_disk
    assert mirror.start() == {"started": True}
    mirror.join(30)
    copied = conn.execute("SELECT COUNT(*) FROM mirror_copies").fetchone()[0]
    assert copied > 0
    verified = conn.execute("SELECT COUNT(*) FROM copies WHERE destination='mirror' "
                            "AND state='verified'").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT f.digest) FROM mirror_copies m JOIN assets a "
                            "ON a.root=m.root AND a.rel_path=m.rel_path JOIN "
                            "content_fingerprints f ON f.asset_id=a.id").fetchone()[0]
    assert verified == distinct > 0


@pytest.fixture()
def away(scanned, tmp_path):
    from ninaivu.cloud import keyring
    from ninaivu.cloud.offsite import Offsite
    cfg, conn, _ = scanned
    for root in cfg.roots:
        fingerprints.fill(conn, root)
    keyring.create(cfg.state_dir, "correct horse battery", "correct horse battery")
    cfg.offsite_kind = "folder"
    cfg.offsite_folder = str(tmp_path / "friends-disk")
    (tmp_path / "friends-disk").mkdir()
    offsite = Offsite(cfg, lambda: db.connect(cfg.db_path))
    offsite.start()
    offsite.join(60)
    assert not offsite.status()["error"]
    return cfg, conn, offsite


def test_the_offsite_copy_writes_the_ledger_as_it_sends(away):
    _, conn, _ = away
    sent = conn.execute("SELECT COUNT(*) FROM offsite_copies WHERE uploaded_at > 0").fetchone()[0]
    verified = conn.execute("SELECT COUNT(*) FROM copies WHERE destination='offsite' "
                            "AND state='verified'").fetchone()[0]
    assert sent > 0 and 0 < verified <= sent


def test_the_offsite_test_restore_brings_back_a_sample_and_checks_it(away, tmp_path):
    cfg, conn, offsite = away
    before = sorted(p.name for p in Path(cfg.state_dir).iterdir())
    done = copies.OffsiteDestination(offsite).restore_test()
    assert done["started"] is True and done["ok"] is True and done["checked"] == 3
    assert all(f["checked"] == fingerprints.ALGO for f in done["files"])
    # The temporary folder went with it.
    assert sorted(p.name for p in Path(cfg.state_dir).iterdir()) == before
    # A fingerprint that does not match what comes back is said.
    first = lambda pool, n: sorted(pool, key=lambda e: e["path"])[:n]  # noqa: E731
    rel = first(offsite_entries(offsite), 1)[0]["path"]
    conn.execute("UPDATE content_fingerprints SET digest=? WHERE asset_id IN "
                 "(SELECT id FROM assets WHERE rel_path=?)", ("0" * 64, rel))
    conn.commit()
    bad = offsite.restore_sample(1, pick=first)
    assert bad["ok"] is False and bad["files"][0]["why"] == "the fingerprint differs"


def offsite_entries(offsite):
    from ninaivu.cloud.offsite import latest, read_manifest
    key, _ = offsite._key()                                     # noqa: SLF001
    return latest(read_manifest(offsite.target(), key,
                                Path(offsite.cfg.state_dir) / "offsite-staging"))


# --- the Secondary's test restore, over the signed peer API -------------------------

from test_mugil_two_computers import primary, run, secondary  # noqa: E402,F401  (fixtures)


def test_the_secondary_reads_back_a_sample_and_reports_fingerprints(  # noqa: F811
        primary, secondary, scanned):  # noqa: F811
    mine, keeper = primary
    _, folder = secondary
    cfg, conn, _ = scanned
    assert run(keeper)["complete"] is True
    # The Secondary's acknowledgements are in the Primary's ledger.
    assert ledger.counts(conn, "secondary")["verified"] > 0
    done = copies.SecondaryDestination(keeper).restore_test()
    assert done["started"] is True and done["ok"] is True and done["checked"] == 3
    # A kept file gone bad on the Secondary is found: same size, other bytes.
    with courier.sent_record(mine.state_dir) as record:
        rel = record.execute("SELECT rel_path FROM sent ORDER BY rel_path").fetchone()[0]
    bad = next(p for p in folder.rglob("*") if p.is_file() and p.as_posix().endswith(rel))
    data = bytearray(bad.read_bytes())
    data[-1] ^= 0xFF
    bad.write_bytes(bytes(data))
    first = lambda rows, n: sorted(rows, key=lambda r: r["rel_path"])[:n]  # noqa: E731
    found = keeper.read_back(1, pick=first)
    assert found["ok"] is False and found["files"] == [
        {"path": rel, "ok": False, "why": "read back different"}]


def test_the_read_back_is_signed_and_the_secondarys_alone(app, secondary):  # noqa: F811
    assert app.test_client().post("/api/mugil/originals/read-back",
                                  json={"items": []}).status_code in (401, 403)


def test_one_computer_has_no_secondary_to_test(cfg, scanned):
    keeper = courier.Courier(cfg, lambda: db.connect(cfg.db_path))
    assert keeper.read_back()["started"] is False
