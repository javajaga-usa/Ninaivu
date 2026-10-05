"""How many copies of each photograph there are, and where."""

from __future__ import annotations


from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.cloud import store
from ninaivu.server import auth
from ninaivu.storage import copies


def _setup(scanned, tmp_path):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    return cfg, conn, services


def test_counts_follow_the_library_drive_and_the_other_disk(scanned, tmp_path):
    cfg, conn, services = _setup(scanned, tmp_path)
    root = cfg.active_root
    total = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed=0").fetchone()[0]
    first = copies.summary(conn, [root])
    assert first["copies"]["1"]["files"] == total and first["copies"]["3"]["files"] == 0
    assert first["single_reasons"]["unqueued"]["files"] == total

    services.cloud.queue_library()
    rows = conn.execute("SELECT rel_path, size, mtime FROM assets WHERE trashed=0 "
                        "ORDER BY rel_path").fetchall()
    for i, row in enumerate(rows[:6]):
        store.record_done(conn, root, row["rel_path"], remote_id=f"file-{i}")
    services.mirror._db()                                           # noqa: SLF001
    for row in rows[:3]:
        conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime) VALUES(?,?,?,?)",
                     (root, row["rel_path"], row["size"], row["mtime"]))
    conn.commit()
    report = copies.summary(conn, [root])
    assert report["copies"]["3"]["files"] == 3
    assert report["copies"]["2"]["files"] == 3
    assert report["copies"]["1"]["files"] == total - 6
    assert report["in_drive"]["files"] == 6 and report["on_disk"]["files"] == 3
    assert report["single_reasons"]["waiting"]["files"] == total - 6


def test_missing_from_drive_is_not_a_copy(scanned, tmp_path):
    cfg, conn, services = _setup(scanned, tmp_path)
    root = cfg.active_root
    services.cloud.queue_library()
    rels = [r["rel_path"] for r in conn.execute("SELECT rel_path FROM assets ORDER BY rel_path")]
    store.record_done(conn, root, rels[0], remote_id="gone-1")
    store.record_done(conn, root, rels[1], remote_id="there-1")
    report = copies.summary(conn, [root], drive_missing=["gone-1"])
    assert report["in_drive"]["files"] == 1
    assert report["single_reasons"]["missing"]["files"] == 1
    listed = copies.single(conn, [root], ["gone-1"], reason="missing")
    assert [i["path"] for i in listed["items"]] == [rels[0]]


def test_a_changed_file_is_not_counted_as_on_the_other_disk(scanned, tmp_path):
    cfg, conn, services = _setup(scanned, tmp_path)
    root = cfg.active_root
    services.mirror._db()                                           # noqa: SLF001
    row = conn.execute("SELECT rel_path, size, mtime FROM assets LIMIT 1").fetchone()
    conn.execute("INSERT INTO mirror_copies(root, rel_path, size, mtime) VALUES(?,?,?,?)",
                 (root, row["rel_path"], row["size"] + 1, row["mtime"]))
    conn.commit()
    assert copies.summary(conn, [root])["on_disk"]["files"] == 0


def test_the_console_and_the_safety_page_say_it(scanned, tmp_path):
    cfg, conn, services = _setup(scanned, tmp_path)
    client = login(create_admin_app(services).test_client(), *ADMIN)
    summary = client.get("/api/copies").get_json()
    assert summary["copies"]["1"]["files"] > 0
    single = client.get("/api/copies/single?limit=3").get_json()
    assert len(single["items"]) == 3 and single["total"] == summary["copies"]["1"]["files"]
    sizes = [i["size"] for i in single["items"]]
    assert sizes == sorted(sizes, reverse=True)
    check = next(c for c in services.safety.report(fresh=True)["checks"] if c["id"] == "copies")
    assert check["status"] == "problem" and "only in the library" in check["summary"]


def test_the_offsite_copy_is_a_place_too(scanned, tmp_path):
    cfg, conn, services = _setup(scanned, tmp_path)
    root = cfg.active_root
    rows = conn.execute("SELECT rel_path, size, mtime FROM assets WHERE trashed=0 "
                        "ORDER BY rel_path").fetchall()
    copies.summary(conn, [root])                     # makes the tables
    conn.execute("INSERT INTO offsite_copies(root, rel_path, size, mtime, object) VALUES(?,?,?,?,?)",
                 (root, rows[0]["rel_path"], rows[0]["size"], rows[0]["mtime"], "files/aa/x.ninaivu"))
    conn.execute("INSERT INTO offsite_copies(root, rel_path, size, mtime, object) VALUES(?,?,?,?,?)",
                 (root, rows[1]["rel_path"], rows[1]["size"] + 1, rows[1]["mtime"], "files/bb/y.ninaivu"))
    conn.commit()
    report = copies.summary(conn, [root])
    assert report["offsite"]["files"] == 1, "a copy of an older version is not a copy"
    assert report["copies"]["2"]["files"] == 1
    single = copies.single(conn, [root])
    assert rows[0]["rel_path"] not in {i["path"] for i in single["items"]}
    services.stop(timeout=5.0)
