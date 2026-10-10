"""Where the space goes: the library by year, kind, camera and folder."""

from conftest import ADMIN, FAMILY, login
from ninaivu import build_services, create_admin_app, create_home_app
from ninaivu.cloud import store
from ninaivu.server import auth


def _console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    services = build_services(cfg)
    services.scanner.stop()
    return services, cfg, conn


def test_the_breakdowns_each_add_up_to_the_whole_library(scanned):
    services, cfg, conn = _console(scanned)
    client = login(create_admin_app(services).test_client(), *ADMIN)
    report = client.get("/api/admin/storage-report").get_json()
    files, size = conn.execute(
        "SELECT COUNT(*), SUM(size) FROM assets WHERE trashed=0").fetchone()
    assert report["files"] == files and report["bytes"] == size
    for name in ("by_kind", "by_year", "by_camera", "by_folder", "by_type"):
        assert sum(r["files"] for r in report[name]) == files, name
        assert sum(r["bytes"] for r in report[name]) == size, name
    assert {r["label"] for r in report["by_folder"]} >= {"2023", "private", "shared", "misc"}
    assert [r["label"] for r in report["by_year"]] == sorted(
        (r["label"] for r in report["by_year"]), reverse=True)
    assert any("TestCam" in r["label"] for r in report["by_camera"])
    assert report["backed_up_bytes"] == 0


def test_it_says_how_much_of_each_has_gone_to_the_cloud(scanned):
    services, cfg, conn = _console(scanned)
    client = login(create_admin_app(services).test_client(), *ADMIN)
    services.cloud.queue_library()
    rows = conn.execute("SELECT rel_path, size FROM assets WHERE rel_path LIKE 'shared/%'").fetchall()
    for i, row in enumerate(rows):
        store.record_done(conn, cfg.active_root, row["rel_path"], remote_id=f"file-{i}")
    report = client.get("/api/admin/storage-report").get_json()
    shared = next(r for r in report["by_folder"] if r["label"] == "shared")
    assert shared["backed_up_bytes"] == shared["bytes"] == sum(r["size"] for r in rows)
    assert report["backed_up_bytes"] == shared["bytes"]


def test_a_long_tail_is_folded_into_one_row(scanned):
    services, cfg, conn = _console(scanned)
    client = login(create_admin_app(services).test_client(), *ADMIN)
    for i in range(20):
        conn.execute("INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, camera) "
                     "VALUES(?,?,?,?,?,?,?,?)", (cfg.active_root, f"cams/c{i}.jpg", f"c{i}.jpg",
                                                  "cams", "jpg", "picture", 1000 + i, f"Camera {i}"))
    conn.commit()
    cameras = client.get("/api/admin/storage-report").get_json()["by_camera"]
    assert len(cameras) == 13 and cameras[-1]["rest"] is True and "others" in cameras[-1]["label"]


def test_it_is_for_administrators_on_the_console_only(scanned):
    services, cfg, conn = _console(scanned)
    assert login(create_home_app(services).test_client(), *ADMIN).get(
        "/api/admin/storage-report").status_code == 404
    assert create_admin_app(services).test_client().get(
        "/api/admin/storage-report").status_code == 401


def test_a_finished_upload_shows_at_once(scanned):
    """The report is remembered between visits; an upload finishing after it
    was read must still be in the next one."""
    services, cfg, conn = _console(scanned)
    client = login(create_admin_app(services).test_client(), *ADMIN)
    assert client.get("/api/admin/storage-report").get_json()["backed_up_bytes"] == 0
    services.cloud.queue_library()
    row = conn.execute("SELECT rel_path, size FROM assets WHERE trashed=0 LIMIT 1").fetchone()
    store.record_done(conn, cfg.active_root, row["rel_path"], remote_id="file-1")
    assert client.get("/api/admin/storage-report").get_json()["backed_up_bytes"] == row["size"]
