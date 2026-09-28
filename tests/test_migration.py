"""Moving Ninaivu to another machine, and telling it where the library went.

A drive that was ``E:`` here comes up as ``D:`` there. Every row in ``assets``
records an absolute root and every thumbnail is *named* after one, so without
this the index points at nothing, every thumbnail is orphaned, and the next
scan reads the lot as a library that has been emptied.

What matters in these is that the work is kept: the same item count, the same
thumbnails under their new names, albums and face groups untouched — and that
a dry run genuinely changes nothing, because that is the only honest way to
offer something that rewrites every path in an index.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ninaivu.media.media import thumb_base
from ninaivu.server import auth
from ninaivu.api import migration_api
from ninaivu.storage import db
from ninaivu.storage import reroot as reroot_kit
from ninaivu import build_services, create_admin_app, create_home_app

from conftest import ADMIN, FAMILY, GUEST, login


# --- a small library, indexed ---------------------------------------------

@pytest.fixture()
def moved(scanned, tmp_path):
    """A scanned library, and the folder it is about to 'appear' at."""
    cfg, conn, _ = scanned
    # The settings file only exists once something has saved; these are about
    # it being rewritten, so it has to be there to rewrite.
    cfg.save()
    return cfg, conn, str(tmp_path / "somewhere-else")


@pytest.fixture()
def console(scanned):
    """The admin app, signed in. Migration is console-only, like the Archive."""
    cfg, conn, _ = scanned
    cfg.watch = False
    cfg.save()
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    yield login(app.test_client(), *ADMIN), cfg, services
    services.stop(timeout=5.0)


def thumbs_on_disk(state_dir) -> set[str]:
    root = Path(state_dir) / "thumbs"
    return {str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*") if p.is_file()}


def items(conn, root: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM assets WHERE root=?",
                        (root,)).fetchone()[0]


# --- the dry run -----------------------------------------------------------

def test_a_dry_run_changes_nothing_at_all(moved):
    cfg, conn, new_root = moved
    old_root = cfg.active_root
    before_thumbs = thumbs_on_disk(cfg.state_dir)
    before_rows = items(conn, old_root)
    before_config = Path(cfg.state_dir, "config.json").read_text(encoding="utf-8")

    report = reroot_kit.reroot(cfg.state_dir, old_root, new_root, dry_run=True)

    assert report.dry_run is True
    assert report.items == before_rows
    assert report.thumbnails > 0
    assert thumbs_on_disk(cfg.state_dir) == before_thumbs
    assert items(conn, old_root) == before_rows
    assert Path(cfg.state_dir, "config.json").read_text(encoding="utf-8") == before_config


def test_a_dry_run_says_when_the_new_folder_is_not_there(moved):
    cfg, _, new_root = moved
    report = reroot_kit.reroot(cfg.state_dir, cfg.active_root, new_root,
                               dry_run=True)
    assert any("not a folder on this machine" in w for w in report.warnings)


# --- the move itself -------------------------------------------------------

def test_every_item_follows_the_library(moved):
    cfg, conn, new_root = moved
    old_root = cfg.active_root
    before = items(conn, old_root)
    assert before > 0

    report = reroot_kit.reroot(cfg.state_dir, old_root, new_root, dry_run=False)

    assert report.items == before
    assert items(conn, new_root) == before
    assert items(conn, old_root) == 0


def test_the_thumbnails_are_renamed_rather_than_rebuilt(moved):
    """The expensive half of a first scan, kept."""
    cfg, conn, new_root = moved
    old_root = cfg.active_root
    rows = conn.execute(
        "SELECT rel_path FROM assets WHERE root=? AND thumb IS NOT NULL "
        "AND thumb <> ''", (old_root,)).fetchall()
    assert rows, "the fixture library has no thumbnails to move"
    before = len(thumbs_on_disk(cfg.state_dir))

    reroot_kit.reroot(cfg.state_dir, old_root, new_root, dry_run=False)

    # Same number of files, at the names the new root hashes to.
    assert len(thumbs_on_disk(cfg.state_dir)) == before
    for (rel_path,) in rows:
        wanted = thumb_base(new_root, rel_path)
        assert any(name.startswith(wanted) for name in thumbs_on_disk(cfg.state_dir)), rel_path


def test_the_index_points_at_the_new_name_for_each_thumbnail(moved):
    cfg, conn, new_root = moved
    old_root = cfg.active_root
    reroot_kit.reroot(cfg.state_dir, old_root, new_root, dry_run=False)
    for rel_path, thumb in conn.execute(
            "SELECT rel_path, thumb FROM assets WHERE root=? AND thumb <> ''",
            (new_root,)):
        assert thumb == thumb_base(new_root, rel_path)


def test_the_settings_follow_too(moved):
    cfg, _, new_root = moved
    old_root = cfg.active_root
    report = reroot_kit.reroot(cfg.state_dir, old_root, new_root, dry_run=False)
    assert report.config_updated is True
    stored = json.loads(Path(cfg.state_dir, "config.json").read_text(encoding="utf-8"))
    assert new_root in stored["roots"]
    assert old_root not in stored["roots"]
    assert stored["active_root"] == new_root


def test_moving_twice_is_not_a_disaster(moved, tmp_path):
    """Somebody will press it again. It must be able to find its own work."""
    cfg, conn, second = moved
    old_root = cfg.active_root
    third = str(tmp_path / "third-place")
    before = items(conn, old_root)

    reroot_kit.reroot(cfg.state_dir, old_root, second, dry_run=False)
    reroot_kit.reroot(cfg.state_dir, second, third, dry_run=False)

    assert items(conn, third) == before
    for rel_path, thumb in conn.execute(
            "SELECT rel_path, thumb FROM assets WHERE root=? AND thumb <> ''",
            (third,)):
        assert thumb == thumb_base(third, rel_path)


# --- what it refuses -------------------------------------------------------

def test_a_folder_the_index_has_never_heard_of_is_refused(moved):
    cfg, _, new_root = moved
    with pytest.raises(reroot_kit.Refused) as refusal:
        reroot_kit.reroot(cfg.state_dir, "Z:\\nothing-here", new_root,
                          dry_run=True)
    assert "Nothing in the index" in str(refusal.value)


def test_moving_a_library_onto_itself_is_refused(moved):
    """366,000 renames to arrive where it started."""
    cfg, _, _ = moved
    with pytest.raises(reroot_kit.Refused):
        reroot_kit.reroot(cfg.state_dir, cfg.active_root, cfg.active_root,
                          dry_run=True)


def test_the_index_spelling_wins_over_the_one_typed(moved):
    """Windows says E:\\x and e:\\X are one folder. SQLite does not."""
    cfg, conn, new_root = moved
    old_root = cfg.active_root
    report = reroot_kit.reroot(cfg.state_dir, old_root.upper(), new_root,
                               dry_run=True)
    assert report.old_root == old_root


# --- through the console ---------------------------------------------------
#
# Migration rewrites every path in the index and renames every thumbnail, so
# it lives on the console and nowhere else — the same rule as the Archive and
# the Cloud tab.

def test_the_page_lists_what_to_copy(console):
    client, _, _ = console
    body = client.get("/api/admin/migration").get_json()
    kinds = {piece["id"].split(":")[0] for piece in body["pieces"]}
    assert {"state", "models", "library"} <= kinds
    assert body["recorded_roots"], "the index should name its library folders"


def test_sizes_are_only_measured_when_asked_for(console):
    """Walking a few hundred thousand thumbnails is seconds; the page is
    useful before that finishes."""
    client, _, _ = console
    plain = client.get("/api/admin/migration").get_json()
    assert "bytes" not in plain["pieces"][0]
    measured = client.get("/api/admin/migration?sizes=1").get_json()
    assert measured["pieces"][0]["bytes"] >= 0
    assert measured["pieces"][0]["files"] >= 0


def test_the_console_dry_run_changes_nothing(console, tmp_path):
    client, cfg, _ = console
    conn = db.connect(cfg.db_path)
    old_root = cfg.active_root
    before = items(conn, old_root)
    body = client.post("/api/admin/migration/reroot", json={
        "from": old_root, "to": str(tmp_path / "elsewhere"), "dry_run": True,
    }).get_json()
    assert body["dry_run"] is True
    assert body["items"] == before
    assert items(conn, old_root) == before


def test_the_console_can_carry_it_out(console, tmp_path):
    client, cfg, _ = console
    conn = db.connect(cfg.db_path)
    old_root = cfg.active_root
    new_root = str(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").mkdir()           # copied there, as a move is
    before = items(conn, old_root)

    body = client.post("/api/admin/migration/reroot", json={
        "from": old_root, "to": new_root, "dry_run": False,
    }).get_json()

    assert body["ok"] is True
    assert items(conn, new_root) == before
    # The settings this process is running on, not only the ones on disk:
    # everything from here until a restart reads these.
    assert new_root in cfg.roots
    assert cfg.active_root == new_root


def test_a_folder_the_index_does_not_know_is_a_plain_refusal(console, tmp_path):
    """Not a 500: somebody typed a path, and that is an ordinary mistake."""
    client, _, _ = console
    response = client.post("/api/admin/migration/reroot", json={
        "from": str(tmp_path / "never-indexed"), "to": str(tmp_path / "x"),
        "dry_run": True,
    })
    assert response.status_code == 400
    assert "Nothing in the index" in response.get_json()["error"]


def test_both_folders_are_required(console):
    client, _, _ = console
    response = client.post("/api/admin/migration/reroot",
                           json={"from": "", "to": "", "dry_run": True})
    assert response.status_code == 400


def test_a_consolidation_stops_it(console, tmp_path, monkeypatch):
    """It is copying files between the very folders this would rename."""
    from ninaivu.archive import scanner as archive_scanner

    client, cfg, _ = console
    monkeypatch.setattr(archive_scanner, "is_scanning", lambda: True)
    response = client.post("/api/admin/migration/reroot", json={
        "from": cfg.active_root, "to": str(tmp_path / "x"), "dry_run": False,
    })
    assert response.status_code == 409
    assert "consolidation" in response.get_json()["error"]


def test_the_indexer_is_held_while_it_runs(console, tmp_path):
    """The one job that would actively fight this, stood down by name."""
    client, cfg, services = console
    scanner = services.scanner
    held: list[str] = []
    original = scanner.defer
    scanner.defer = lambda reason: (held.append(reason), original(reason))[1]
    (tmp_path / "elsewhere").mkdir()
    try:
        client.post("/api/admin/migration/reroot", json={
            "from": cfg.active_root, "to": str(tmp_path / "elsewhere"),
            "dry_run": False,
        })
    finally:
        scanner.defer = original
    assert held == [migration_api.CLAIM_REROOT]
    assert scanner.deferred is None, "and it was let go again afterwards"


def test_a_folder_that_is_not_there_is_refused(console, tmp_path):
    """A mistyped path emptied the gallery and renamed every thumbnail."""
    client, cfg, _ = console
    conn = db.connect(cfg.db_path)
    before = items(conn, cfg.active_root)
    response = client.post("/api/admin/migration/reroot", json={
        "from": cfg.active_root, "to": str(tmp_path / "typo"), "dry_run": False,
    })
    assert response.status_code == 400
    assert items(conn, cfg.active_root) == before


def test_a_folder_the_library_picker_refuses_is_refused_here_too(console):
    """The system-folder refusal (and --lock-roots) applied everywhere else a
    library is chosen; this was the way around them."""
    import os
    client, cfg, _ = console
    system = os.environ.get("SystemRoot", "/etc")
    response = client.post("/api/admin/migration/reroot", json={
        "from": cfg.active_root, "to": system, "dry_run": False,
    })
    assert response.status_code == 400


# --- who may reach it ------------------------------------------------------

def test_the_family_app_does_not_route_migration_at_all(scanned):
    """404 on port 5000: absent, not forbidden."""
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    try:
        home = login(create_home_app(services).test_client(), *ADMIN)
        assert home.get("/api/admin/migration").status_code == 404
        assert home.post("/api/admin/migration/reroot",
                         json={"from": "a", "to": "b"}).status_code == 404
    finally:
        services.stop(timeout=5.0)


@pytest.mark.parametrize("who,role", [(FAMILY, auth.ROLE_FAMILY),
                                      (GUEST, auth.ROLE_GUEST)])
def test_nobody_but_an_administrator_may_move_a_library(scanned, who, role):
    """They cannot reach the routes because they cannot reach the console.

    Stronger than a per-route check, and worth pinning as the reason: the
    console turns a family member away at sign-in, so there is no session in
    which these endpoints exist for them at all.
    """
    cfg, conn, _ = scanned
    cfg.watch = False
    admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    auth.create_user(conn, who[0], who[1], display_name=who[0].title(),
                     role=role, created_by=admin.id)
    services = build_services(cfg)
    services.scanner.stop()
    try:
        client = create_admin_app(services).test_client()
        signed_in = client.post("/api/auth/login",
                                json={"username": who[0], "password": who[1]})
        assert signed_in.status_code == 403
        # And without a session, so is everything behind one.
        assert client.get("/api/admin/migration").status_code in (401, 403)
        assert client.post("/api/admin/migration/reroot", json={
            "from": "a", "to": "b", "dry_run": False,
        }).status_code in (401, 403)
    finally:
        services.stop(timeout=5.0)
