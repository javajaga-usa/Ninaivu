"""Media that was hidden on disk stays hidden in the library.

Somebody went to the trouble of putting these files out of sight. Skipping
them loses photographs; showing them publishes what was deliberately hidden.
Indexing them as admin-only is the only reading that respects both, and these
tests pin every edge of that decision.
"""

import os
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.server import auth
from ninaivu.storage import db
from ninaivu.archive.scanner import source_is_hidden
from ninaivu.server.auth import VIS_FAMILY, VIS_HIDDEN, VIS_PUBLIC
from ninaivu.server.config import Config
from ninaivu.media.scanner import Scanner, is_hidden, path_is_hidden, walk_media, _dir_is_hidden


def shot(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 150), (90, 120, 60)).save(path)
    return path


@pytest.fixture()
def mixed(tmp_path):
    """Ordinary media, a hidden file, and media inside a hidden folder."""
    root = tmp_path / "lib"
    shot(root / "holiday/open.jpg")
    shot(root / ".private/secret.jpg")
    shot(root / ".private/deeper/also_secret.jpg")
    shot(root / "holiday/.quiet.jpg")
    return root


def build(root, tmp_path, **over):
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.roots = [str(root)]; cfg.active_root = str(root)
    cfg.ai_enabled = False; cfg.ai_engine = "off"; cfg.watch = False
    cfg.open_browsing = True; cfg.min_media_bytes = 0
    for key, value in over.items():
        setattr(cfg, key, value)
    cfg.ensure_dirs()
    return cfg


def scan(cfg):
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    Scanner(cfg)._run(Path(cfg.active_root), full=True)
    return conn


def rows(conn):
    return {r["rel_path"]: r for r in conn.execute(
        "SELECT rel_path, visibility, vis_source FROM assets")}


# --- the walk now finds them ----------------------------------------------

def test_hidden_media_is_indexed_rather_than_skipped(mixed, tmp_path):
    cfg = build(mixed, tmp_path)
    found = {rel for rel, _ in walk_media(mixed, cfg)}
    assert found == {
        "holiday/open.jpg", "holiday/.quiet.jpg",
        ".private/secret.jpg", ".private/deeper/also_secret.jpg",
    }, found


def test_caches_and_recycle_bins_are_still_skipped(tmp_path):
    root = tmp_path / "lib"
    shot(root / "real.jpg")
    shot(root / ".cache/thumb.jpg")
    shot(root / "$RECYCLE.BIN/deleted.jpg")
    shot(root / "node_modules/pkg/logo.jpg")
    cfg = build(root, tmp_path)
    assert {rel for rel, _ in walk_media(root, cfg)} == {"real.jpg"}


# --- and they land hidden --------------------------------------------------

def test_a_hidden_file_is_indexed_as_hidden(mixed, tmp_path):
    conn = scan(build(mixed, tmp_path))
    item = rows(conn)["holiday/.quiet.jpg"]
    assert item["visibility"] == VIS_HIDDEN
    assert item["vis_source"] == "hidden"


def test_media_inside_a_hidden_folder_is_hidden(mixed, tmp_path):
    conn = scan(build(mixed, tmp_path))
    assert rows(conn)[".private/secret.jpg"]["visibility"] == VIS_HIDDEN


def test_the_hidden_folder_carries_down_to_nested_folders(mixed, tmp_path):
    conn = scan(build(mixed, tmp_path))
    item = rows(conn)[".private/deeper/also_secret.jpg"]
    assert item["visibility"] == VIS_HIDDEN, (
        "a plainly-named subfolder of a hidden folder is still hidden")


def test_ordinary_media_is_unaffected(mixed, tmp_path):
    conn = scan(build(mixed, tmp_path))
    item = rows(conn)["holiday/open.jpg"]
    assert item["visibility"] == VIS_FAMILY
    assert item["vis_source"] == "default"


# --- an admin's own decision still wins ------------------------------------

def test_an_explicit_folder_rule_outranks_the_filesystem(mixed, tmp_path):
    cfg = build(mixed, tmp_path)
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    db.set_folder_visibility(conn, str(mixed), ".private", VIS_PUBLIC, admin.id)
    Scanner(cfg)._run(Path(cfg.active_root), full=True)

    item = rows(conn)[".private/secret.jpg"]
    assert item["visibility"] == VIS_PUBLIC, (
        "an admin who deliberately published this folder was overruled by a dot")
    assert item["vis_source"] == "folder"


def test_an_admin_can_unhide_one_item_and_a_rescan_respects_it(mixed, tmp_path):
    cfg = build(mixed, tmp_path)
    conn = scan(cfg)
    target = conn.execute(
        "SELECT id FROM assets WHERE rel_path=?", (".private/secret.jpg",)
    ).fetchone()["id"]
    db.set_visibility(conn, [target], VIS_FAMILY)
    Scanner(cfg)._run(Path(cfg.active_root), full=True)
    assert rows(conn)[".private/secret.jpg"]["visibility"] == VIS_FAMILY, (
        "a rescan re-hid an item the admin had deliberately shown")


# --- who can actually see them ---------------------------------------------

def test_the_family_never_sees_hidden_media(mixed, tmp_path):
    from ninaivu import create_app
    cfg = build(mixed, tmp_path)
    conn = scan(cfg)
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    app = create_app(cfg)
    app.config["MV_SCANNER"].stop()

    family = login(app.test_client(), *FAMILY)
    seen = {i["name"] for i in family.get("/api/assets?limit=99").get_json()["items"]}
    assert seen == {"open.jpg"}, seen

    console = login(app.test_client(), *ADMIN)
    admin_seen = {i["name"] for i in console.get(
        "/api/assets?limit=99&visibility=hidden").get_json()["items"]}
    assert "secret.jpg" in admin_seen, "the admin cannot reach what was indexed"


# --- the switch -------------------------------------------------------------

def test_hidden_media_can_be_skipped_entirely(mixed, tmp_path):
    cfg = build(mixed, tmp_path, index_hidden=False)
    assert {rel for rel, _ in walk_media(mixed, cfg)} == {"holiday/open.jpg"}


def test_the_default_is_to_index_hidden_media():
    assert Config().index_hidden is True


# --- the primitives ---------------------------------------------------------

def test_is_hidden_reads_the_windows_attribute():
    class FakeStat:
        st_file_attributes = 0x2      # FILE_ATTRIBUTE_HIDDEN
    assert is_hidden("Holiday.jpg", FakeStat()) is True
    assert is_hidden("Holiday.jpg", None) is False


def test_is_hidden_reads_the_system_attribute():
    class FakeStat:
        st_file_attributes = 0x4      # FILE_ATTRIBUTE_SYSTEM
    assert is_hidden("Holiday.jpg", FakeStat()) is True


def test_path_is_hidden_checks_every_folder_on_the_way_down(mixed):
    assert path_is_hidden(mixed, ".private/deeper/also_secret.jpg") is True
    assert path_is_hidden(mixed, "holiday/open.jpg") is False
    assert path_is_hidden(mixed, "holiday/.quiet.jpg") is True


def test_dir_is_hidden_ignores_drive_root():
    assert _dir_is_hidden("C:\\") is False
    assert _dir_is_hidden("C:") is False
    assert _dir_is_hidden("/") is False
    assert _dir_is_hidden("") is False


def test_source_is_hidden_ignores_drive_root(tmp_path):
    normal_file = tmp_path / "photos" / "pic.jpg"
    shot(normal_file)
    assert source_is_hidden(str(normal_file)) is False

    hidden_dot_file = tmp_path / ".private" / "pic.jpg"
    shot(hidden_dot_file)
    assert source_is_hidden(str(hidden_dot_file)) is True


# --- a hidden item must be invisible on every route, not just the listing ---

@pytest.fixture()
def household(mixed, tmp_path):
    """A scanned library with a hidden folder, and one profile per role."""
    from ninaivu import create_app
    cfg = build(mixed, tmp_path)
    conn = scan(cfg)
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                     role=auth.ROLE_FAMILY, created_by=admin.id)
    auth.create_user(conn, GUEST[0], GUEST[1], display_name="Neighbour",
                     role=auth.ROLE_GUEST, created_by=admin.id)
    app = create_app(cfg)
    app.config["MV_SCANNER"].stop()
    hidden_id = conn.execute(
        "SELECT id FROM assets WHERE rel_path=?", (".private/secret.jpg",)
    ).fetchone()["id"]
    return app, conn, cfg, hidden_id


@pytest.mark.parametrize("route", [
    "/api/asset/{id}", "/api/thumb/{id}", "/api/file/{id}", "/api/download/{id}",
])
@pytest.mark.parametrize("who", ["family", "guest", "anon"])
def test_no_route_serves_a_hidden_item_to_a_non_admin(household, route, who):
    app, _, _, hidden_id = household
    client = (app.test_client() if who == "anon"
              else login(app.test_client(), *(FAMILY if who == "family" else GUEST)))
    response = client.get(route.format(id=hidden_id))
    # 404 is the intended answer — a hidden item should be indistinguishable
    # from one that does not exist. /api/download additionally refuses guests
    # outright with 401, which is stricter, not weaker. What must never happen
    # is 200.
    assert response.status_code in (401, 403, 404), (
        f"{who} reached a hidden photo via {route}: {response.status_code}")
    assert response.status_code != 200


def test_a_hidden_item_is_absent_from_every_listing(household):
    app, _, _, hidden_id = household
    family = login(app.test_client(), *FAMILY)
    for path in ("/api/assets?limit=99",
                 f"/api/assets?ids={hidden_id}",
                 "/api/segments",
                 "/api/timeline",
                 "/api/duplicates"):
        body = family.get(path).get_json()
        blob = str(body)
        assert "secret.jpg" not in blob, f"{path} leaked a hidden photo"


def test_the_hidden_folder_is_not_offered_as_a_facet(household):
    app, _, _, _ = household
    family = login(app.test_client(), *FAMILY)
    facets = str(family.get("/api/facets").get_json())
    assert ".private" not in facets, "the folder list advertised a hidden folder"


def test_a_family_member_cannot_add_a_hidden_item_to_an_album(household):
    app, _, _, hidden_id = household
    family = login(app.test_client(), *FAMILY)
    created = family.post("/api/albums", json={"name": "Trip", "ids": [hidden_id]})
    assert created.status_code == 200
    album_id = created.get_json()["id"]
    added = family.post(f"/api/albums/{album_id}/items", json={"ids": [hidden_id]})
    assert added.get_json()["count"] == 0, (
        "a hidden photo was collected into an album by someone who cannot see it")


def test_search_does_not_count_hidden_items(household):
    app, _, _, _ = household
    family = login(app.test_client(), *FAMILY)
    body = family.get("/api/assets?q=secret&limit=99").get_json()
    assert body["total"] == 0, f"the count admitted a hidden photo exists: {body['total']}"


def test_the_admin_can_still_reach_it_and_release_it(household):
    app, conn, cfg, hidden_id = household
    console = login(app.test_client(), *ADMIN)
    assert console.get(f"/api/asset/{hidden_id}").status_code == 200

    payload = console.get(f"/api/asset/{hidden_id}").get_json()
    assert payload["visibility"] == "hidden"
    assert payload["visibility_source"] == "hidden", (
        "an admin cannot tell whether they hid this or the filesystem did")

    assert console.post("/api/visibility",
                        json={"ids": [hidden_id], "visibility": "family"}
                        ).status_code == 200

    family = login(app.test_client(), *FAMILY)
    assert family.get(f"/api/asset/{hidden_id}").status_code == 200, (
        "the admin released it and the family still cannot see it")


def test_a_rule_on_an_ordinary_parent_does_not_expose_a_hidden_subfolder(mixed, tmp_path):
    """A rule set on `holiday` is not a decision about a `.private` inside it."""
    shot(mixed / "holiday/.later/new.jpg")
    cfg = build(mixed, tmp_path)
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    db.set_folder_visibility(conn, str(mixed), "holiday", VIS_PUBLIC, admin.id)
    Scanner(cfg)._run(Path(cfg.active_root), full=True)

    table = rows(conn)
    assert table["holiday/open.jpg"]["visibility"] == VIS_PUBLIC
    assert table["holiday/.later/new.jpg"]["visibility"] == VIS_HIDDEN, (
        "a public rule on the parent published a folder that was hidden on disk")


# --- the archive's check, and the folder cache it now uses -------------------

windows_only = pytest.mark.skipif(os.name != "nt", reason="hidden attribute is Windows-only")


@windows_only
def test_a_folder_hidden_by_attribute_hides_everything_below_it(tmp_path):
    """Not only dot names: a folder hidden the Windows way hides its photos,
    however deep they sit, and a sibling folder is unaffected."""
    from ninaivu.archive.scanner import mark_hidden
    deep = tmp_path / "Pictures" / "Private" / "2014" / "trip" / "pic.jpg"
    open_ = tmp_path / "Pictures" / "Family" / "2014" / "pic.jpg"
    shot(deep)
    shot(open_)
    assert mark_hidden(str(tmp_path / "Pictures" / "Private"))
    for cache in (None, {}):
        assert source_is_hidden(str(deep), cache) is True
        assert source_is_hidden(str(open_), cache) is False


@windows_only
def test_a_file_hidden_by_attribute_is_hidden_in_an_open_folder(tmp_path):
    from ninaivu.archive.scanner import mark_hidden
    visible = tmp_path / "Family" / "a.jpg"
    concealed = tmp_path / "Family" / "b.jpg"
    shot(visible)
    shot(concealed)
    assert mark_hidden(str(concealed))
    cache = {}
    assert source_is_hidden(str(concealed), cache) is True
    # The folder answer is cached; the file's own attribute must still be read,
    # or one hidden photo would decide the answer for its whole folder.
    assert source_is_hidden(str(visible), cache) is False


@windows_only
def test_the_folder_cache_asks_about_each_folder_once(tmp_path, monkeypatch):
    """The point of the cache: a folder of photos costs one question per photo
    plus one per folder above it, not one per folder per photo."""
    import ctypes
    folder = tmp_path / "a" / "b" / "c" / "d"
    photos = [folder / f"IMG_{i}.jpg" for i in range(50)]
    for photo in photos:
        shot(photo)

    real = ctypes.windll.kernel32.GetFileAttributesW
    calls = []

    class Kernel32:
        def GetFileAttributesW(self, path):
            calls.append(path)
            return real(path)

    class WinDLL:
        kernel32 = Kernel32()

    monkeypatch.setattr(ctypes, "windll", WinDLL())
    uncached = [source_is_hidden(str(p)) for p in photos]
    without = len(calls)
    calls.clear()
    cache = {}
    cached = [source_is_hidden(str(p), cache) for p in photos]
    with_cache = len(calls)

    assert cached == uncached == [False] * 50
    depth = len(folder.parts) - 1          # folders above the photo, excluding the drive
    assert with_cache <= len(photos) + depth, (with_cache, without)
    assert with_cache < without / 3, (with_cache, without)
