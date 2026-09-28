"""Regression tests for the fourteen defects found by the whole-project audit.

Each one was written as a *failing* test first, against the code as it stood,
so every assertion here is known to have caught the real fault rather than
merely describing it. They are the reason none of these can come back quietly.
"""
from pathlib import Path

import pytest

from conftest import ADMIN, FAMILY, GUEST, login, ids_of
from ninaivu.server import auth
from ninaivu.storage import db


# --- claim: GET /api/albums is unauthenticated and unscoped ----------------

def test_albums_requires_a_session(anon):
    r = anon.get("/api/albums")
    assert r.status_code in (401, 404), f"anonymous read of every album: {r.get_json()}"


def test_albums_are_scoped_to_the_viewer(app, people, scanned):
    _, conn, _ = scanned
    admin_c = login(app.test_client(), *ADMIN)
    admin_c.post("/api/albums", json={"name": "Private trip"})
    guest_c = login(app.test_client(), *GUEST)
    names = [a["name"] for a in guest_c.get("/api/albums").get_json().get("albums", [])]
    assert "Private trip" not in names, f"guest sees other people's albums: {names}"


# --- claim: album mutations have no ownership check ------------------------

def test_a_family_member_cannot_delete_someone_elses_album(app, people):
    admin_c = login(app.test_client(), *ADMIN)
    album = admin_c.post("/api/albums", json={"name": "Dad's album"}).get_json()
    aid = album.get("id") or album.get("album", {}).get("id")
    fam_c = login(app.test_client(), *FAMILY)
    r = fam_c.delete(f"/api/albums/{aid}")
    assert r.status_code in (403, 404), f"family deleted admin's album: {r.status_code}"


# --- claim: _guard does not check `trashed` -------------------------------

def test_a_trashed_asset_is_not_served_by_id(app, people):
    admin_c = login(app.test_client(), *ADMIN)
    fam_c = login(app.test_client(), *FAMILY)
    target = ids_of(fam_c)[0]
    assert admin_c.post(f"/api/asset/{target}", json={"trashed": True}).status_code == 200
    listed = fam_c.get(f"/api/assets?ids={target}").get_json()["items"]
    assert listed == [], "listing already hides it — good"
    r = fam_c.get(f"/api/asset/{target}")
    assert r.status_code == 404, f"trashed asset still served by id: {r.status_code}"


def test_a_trashed_original_is_not_downloadable(app, people):
    admin_c = login(app.test_client(), *ADMIN)
    fam_c = login(app.test_client(), *FAMILY)
    target = ids_of(fam_c)[0]
    admin_c.post(f"/api/asset/{target}", json={"trashed": True})
    r = fam_c.get(f"/api/file/{target}")
    assert r.status_code == 404, f"trashed original still served: {r.status_code}"


# --- claim: administrative endpoints leak onto the family port ------------

#: The admin actions that live in the gallery on purpose, because performing
#: them means looking at the photographs. Each is behind ``@require_admin``;
#: what makes them different from library management is that no amount of
#: reading a folder tree tells you which photograph to delete, rotate or hide.
#: Listed by name so that adding a fourth is a deliberate edit to this line
#: rather than something that slides in unnoticed.
IN_THE_GALLERY_ON_PURPOSE = {
    "/api/delete",
    "/api/visibility",
    "/api/asset/<int:asset_id>/rotate",
}


def test_library_management_is_absent_from_the_family_port(scanned):
    """Absent, not forbidden — a 404, so there is no surface to attack.

    The rule is about *library* management: people, settings, roots, scanning,
    folder rules, the archive. Those have no business being routed on the port
    the household opens, whatever guard sits in front of them.
    """
    from ninaivu import build_services, create_admin_app, create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()

    home = {str(r) for r in create_home_app(services).url_map.iter_rules()}
    console = {str(r) for r in create_admin_app(services).url_map.iter_rules()}

    # Anything the console has that the family port also has, minus the three
    # gallery actions above, is a candidate leak.
    shared = (console & home) - IN_THE_GALLERY_ON_PURPOSE
    management = {p for p in shared if p.startswith("/api/")
                  and any(p.startswith(f"/api/{seg}") for seg in
                          ("people", "settings", "library", "scan", "admin",
                           "archive", "audit", "recycle"))}
    assert not management, (
        f"library management is routed on the family port: {sorted(management)}")

    for path in IN_THE_GALLERY_ON_PURPOSE:
        assert path in home, f"{path} should be in the gallery and is not"
    assert "/api/visibility/folder" not in home, (
        "folder rules are library management and belong on the console only")
    assert "/api/people" not in home
    assert "/api/admin/overview" not in home


# --- claim: avatars enumerate hidden profiles -----------------------------

# --- claim: scope LIKE pattern is unescaped -------------------------------

def test_scope_does_not_leak_across_wildcard_sibling_folders(scanned):
    """The scope matches its own folder and subtree, and nothing that merely
    resembles it — wildcard look-alikes, prefixes, or a different case."""
    cfg, conn, _ = scanned
    clause, params = db.scope_clause("a", "family_holidays")
    candidates = {
        "family_holidays": True,
        "family_holidays/greece": True,
        "family_holidays/greece/day 2": True,
        "familyXholidays": False,             # _ as a LIKE wildcard
        "familyXholidays/beach": False,
        "family-holidays/beach": False,
        "family_holidays-2019": False,        # sorts inside the range
        "family_holidays 2019/x": False,
        "family_holidays0": False,            # the exclusive upper bound
        "Family_Holidays/beach": False,       # the by-id guard is case-sensitive
        "family": False,
        "": False,
    }
    for folder, expected in candidates.items():
        hit = conn.execute(f"SELECT 1 FROM (SELECT ? AS folder) a WHERE {clause}",
                           (folder, *params)).fetchone() is not None
        assert hit is expected, f"scope family_holidays vs {folder!r}: matched={hit}"


# --- claim: user_assets loses its foreign key on an upgraded database -----

def test_user_assets_keeps_its_cascade_on_an_upgraded_database(scanned):
    """An upgraded database must not be left with favourites that outlive assets."""
    cfg, conn, _ = scanned
    # What _migrate leaves behind on a database that came from v3: the table
    # exists, so AUTH_SCHEMA's CREATE TABLE IF NOT EXISTS never applies, and
    # the constraints are simply absent.
    conn.executescript("""
        DROP TABLE IF EXISTS user_assets;
        CREATE TABLE user_assets (
            user_id  INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,
            rating   INTEGER NOT NULL DEFAULT 0,
            seen_at  REAL,
            PRIMARY KEY (user_id, asset_id)
        );
    """)
    conn.commit()
    auth.init_auth_schema(conn)
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    asset_id = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO user_assets(user_id, asset_id, favorite) VALUES (?,?,1)",
                 (admin.id, asset_id))
    conn.commit()

    db.heal_schema(conn)

    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='user_assets'").fetchone()[0]
    assert "REFERENCES assets" in sql, (
        "user_assets has no foreign key, so a favourite survives its photo and "
        f"re-attaches to whatever reuses the rowid. Schema: {sql}")
    kept = conn.execute("SELECT COUNT(*) FROM user_assets").fetchone()[0]
    assert kept == 1, "the rebuild lost a live favourite"

    # And the cascade now actually fires.
    conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM user_assets").fetchone()[0]
    assert left == 0, "deleting the photo left its favourite behind"


# --- claim: an empty walk deletes every row for that root -----------------

def test_the_cascade_repair_does_not_hang_a_real_start(scanned):
    """The repair runs *inside* ``init_db``, which already holds the write lock.

    The test above calls ``heal_schema`` directly, outside the lock, so it never
    noticed that the repair took the same (non-reentrant) lock again. On a real
    upgraded install every start met all three of the repair's conditions and
    Ninaivu hung before it bound a port.
    """
    import threading

    cfg, conn, _ = scanned
    conn.executescript("""
        DROP TABLE IF EXISTS user_assets;
        CREATE TABLE user_assets (
            user_id  INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            favorite INTEGER NOT NULL DEFAULT 0,
            rating   INTEGER NOT NULL DEFAULT 0,
            seen_at  REAL,
            PRIMARY KEY (user_id, asset_id)
        );
    """)
    conn.commit()
    auth.init_auth_schema(conn)
    db.close_all()

    done = threading.Event()
    result: dict = {}

    def start():
        try:
            fresh = db.init_db(cfg.db_path)
            result["sql"] = fresh.execute(
                "SELECT sql FROM sqlite_master WHERE name='user_assets'").fetchone()[0]
        finally:
            done.set()

    threading.Thread(target=start, daemon=True).start()
    assert done.wait(10), "init_db never returned: the schema repair deadlocked on the write lock"
    assert "REFERENCES assets" in result["sql"], "the start-up path did not repair the table"


def test_a_root_that_reads_empty_does_not_wipe_the_index(scanned):
    """An unplugged drive whose mountpoint survives must not prune the index."""
    import shutil
    from ninaivu.media.scanner import Scanner
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    before = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    assert before > 0

    # An album, so we can see what a prune cascades into.
    album = db.album_create(conn, "Summer") if hasattr(db, "album_create") else None
    aid = album["id"] if isinstance(album, dict) else album
    ids = [r[0] for r in conn.execute("SELECT id FROM assets").fetchall()]
    if aid:
        db.album_add(conn, aid, ids)
    items_before = conn.execute("SELECT COUNT(*) FROM album_items").fetchone()[0]

    # The drive goes away; the mountpoint directory remains, and is empty.
    for child in root.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    assert root.is_dir() and not any(root.iterdir())

    scanner = Scanner(cfg)
    scanner._run(root, full=False)

    after = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    items_after = conn.execute("SELECT COUNT(*) FROM album_items").fetchone()[0]
    assert after == before, (
        f"a root that read empty pruned the index: {before} rows → {after}; "
        f"album membership {items_before} → {items_after}; scanner reported "
        f"status={scanner.progress.status!r} errors={scanner.progress.errors}")


# --- claim: an underscore in a scoped folder name leaks a sibling ---------

def test_a_scoped_member_does_not_see_a_wildcard_sibling_folder(tmp_path):
    """`family_holidays` must not also match `family-holidays`."""
    from PIL import Image
    from ninaivu import create_app
    from ninaivu.server.config import Config

    root = tmp_path / "lib"
    # The sibling needs a subfolder: the pattern is "<scope>/%", so the
    # underscore must be able to match "-" with a real "/" after it.
    for folder, n in (("family_holidays/greece", 2), ("family-holidays/private", 3)):
        d = root / folder
        d.mkdir(parents=True)
        for i in range(n):
            Image.new("RGB", (60, 40), (i * 60, 90, 30)).save(d / f"s{i}.jpg")

    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(root)]; cfg.active_root = str(root)
    cfg.ai_enabled = False; cfg.ai_engine = "off"; cfg.watch = False
    cfg.open_browsing = True; cfg.ensure_dirs()

    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    from ninaivu.media.scanner import Scanner
    Scanner(cfg)._run(root, full=True)

    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id,
                     scope="family_holidays")

    app = create_app(cfg)
    app.config["MV_SCANNER"].stop()
    c = login(app.test_client(), *FAMILY)
    folders = {i["folder"] for i in c.get("/api/assets?limit=99").get_json()["items"]}
    assert all(f.startswith("family_holidays") for f in folders), (
        f"a member scoped to family_holidays also sees "
        f"{ {f for f in folders if not f.startswith('family_holidays')} } "
        "— the underscore is acting as a SQL LIKE wildcard")


# --- claim: /api/avatar enumerates profiles ------------------------------

def test_avatar_does_not_distinguish_a_real_profile_from_a_missing_one(anon, people):
    """404 for both, or 401 for both — never a 200 that confirms existence."""
    real = anon.get(f"/api/avatar/{people['admin'].id}").status_code
    missing = anon.get("/api/avatar/999999").status_code
    assert real == missing, (
        f"profile {people['admin'].id} answers {real} but a missing one answers "
        f"{missing} — anonymous callers can enumerate the household")


def test_a_profile_with_a_picture_is_not_enumerable_anonymously(anon, people, app):
    """A profile the picker hides must not be confirmable via its avatar."""
    import io
    from PIL import Image
    from ninaivu.api.accounts_api import _avatar_dir
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 90, 200)).save(buf, "PNG")
    with app.app_context():
        directory = _avatar_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a1.png").write_bytes(buf.getvalue())
    conn = people["conn"]
    conn.execute("UPDATE users SET avatar='a1.png' WHERE id=?", (people["admin"].id,))
    conn.commit()

    real = anon.get(f"/api/avatar/{people['admin'].id}").status_code
    missing = anon.get("/api/avatar/999999").status_code
    assert real == missing, (
        f"the admin profile — which /api/auth/state deliberately hides from the "
        f"home page — answers {real} while a missing id answers {missing}")


# --- AUDIT-1, the other two triggers, and the cases that must still work ---

def test_an_unreadable_subfolder_stops_the_prune(scanned, monkeypatch):
    """A walk that could not read part of the tree is not evidence of deletion.

    The failure is forced rather than produced with permissions, because the
    suite may be running as a user that permissions do not stop — which would
    quietly turn this into a test of nothing.
    """
    import os as os_mod
    from ninaivu.media import scanner as scanner_mod
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    before = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    # Everything under `shared` really is gone, and one folder cannot be read.
    for child in (root / "shared/holiday").iterdir():
        child.unlink()
    real_scandir = os_mod.scandir
    blocked = str(root / "private")

    def scandir(path=".", *args, **kwargs):
        if str(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(scanner_mod.os, "scandir", scandir)
    scanner = Scanner(cfg)
    scanner._run(root, full=False)

    after = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    assert after == before, "rows were pruned on the strength of a walk that failed"
    assert scanner.skipped_prune is not None
    assert "could not read" in scanner.skipped_prune.reason
    assert scanner.progress.warning


def test_an_ordinary_tidy_up_still_prunes(scanned):
    """The guard must not stop Ninaivu noticing a handful of deleted photos."""
    from ninaivu.media.scanner import Scanner
    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    before = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    (root / "shared/holiday/beach0.jpg").unlink()
    Scanner(cfg)._run(root, full=False)

    after = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    assert after == before - 1, "a normal deletion was not picked up"


def test_a_mass_prune_is_refused_but_can_be_forced(scanned):
    cfg, conn, _ = scanned
    root = str(cfg.active_root)
    rows = [r[0] for r in conn.execute(
        "SELECT rel_path FROM assets WHERE root=?", (root,))]
    for extra in range(len(rows), db.PRUNE_FLOOR + 5):
        conn.execute(
            "INSERT INTO assets(root, rel_path, filename, kind, indexed_at) "
            "VALUES (?,?,?,'picture',0)",
            (root, f"filler/{extra}.jpg", f"{extra}.jpg"))
    conn.commit()
    survivors = rows[:2]

    with pytest.raises(db.PruneRefused):
        db.delete_missing(conn, root, survivors)
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE root=?",
                        (root,)).fetchone()[0] > 2

    db.delete_missing(conn, root, survivors, force=True)
    assert conn.execute("SELECT COUNT(*) FROM assets WHERE root=?",
                        (root,)).fetchone()[0] == 2


# --- AUDIT-6: the archive selection must never widen on its own -------------

def test_an_empty_media_selection_stays_empty():
    from ninaivu.archive.safety import normalise_sources
    assert normalise_sources(["D:/cards"], set())[0]["types"] == frozenset()
    assert normalise_sources(["D:/x"], {"photo"})[0]["types"] == frozenset()
    # …while "the caller did not say" still means everything, as the flat form
    # has always meant.
    assert len(normalise_sources(["D:/y"])[0]["types"]) == 3


def test_the_job_object_does_not_widen_either(tmp_path):
    from ninaivu.archive.scanner import ArchiveJob
    job = ArchiveJob([str(tmp_path)], str(tmp_path / "dest"), media_types=set())
    assert job.media_types == frozenset()
