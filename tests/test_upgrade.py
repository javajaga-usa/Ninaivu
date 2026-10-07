"""Upgrading a household database that already exists.

The bug this file exists for: a family running 3.0.x upgraded, opened the
admin console, and got a 500 — ``no such column: library``. Their database
was already recorded at the current ``SCHEMA_VERSION``, so the version-gated
migration never ran, and ``CREATE TABLE IF NOT EXISTS`` cannot add a column
to a table that is already there. The repair must therefore be
*version-independent*: run on every start, and be a no-op when there is
nothing to fix.
"""

import sqlite3

import pytest

from ninaivu.server import auth
from ninaivu.storage import db


# The users table exactly as 3.0.x created it: no scope, pin, library or
# must_change, because none of those features existed yet.
LEGACY_USERS = """
CREATE TABLE users (
    id           INTEGER PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'family',
    password     TEXT,
    avatar       TEXT,
    color        TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    created_by   INTEGER,
    last_login   REAL
);
"""


@pytest.fixture()
def legacy(tmp_path):
    """A database at the current version whose users table predates it."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY_USERS)
    raw.execute(
        "INSERT INTO users(username, display_name, role, active, created_at) "
        "VALUES('dad', 'Dad', 'admin', 1, 0)"
    )
    raw.commit()
    raw.close()
    db.close_all()
    return path


def test_missing_user_columns_are_added_on_start(legacy):
    conn = db.init_db(legacy)
    assert "library" not in db.columns(conn, "users"), "fixture is not legacy"

    auth.init_auth_schema(conn)

    have = db.columns(conn, "users")
    for column in ("library", "scope", "pin", "must_change"):
        assert column in have, column


def test_the_query_that_crashed_now_runs(legacy):
    """``/api/admin/overview`` counts members assigned to each folder."""
    conn = db.init_db(legacy)
    auth.init_auth_schema(conn)

    row = conn.execute(
        "SELECT COUNT(*) n FROM users WHERE active=1 AND library IS NOT NULL "
        "AND (library = ? OR library LIKE ?)",
        ("C:/Master", "C:/Master%"),
    ).fetchone()
    assert row["n"] == 0


def test_existing_rows_survive_the_repair(legacy):
    conn = db.init_db(legacy)
    auth.init_auth_schema(conn)

    row = conn.execute("SELECT * FROM users WHERE username='dad'").fetchone()
    assert row["display_name"] == "Dad"
    assert row["role"] == "admin"
    assert row["library"] is None, "a pre-existing profile sees everything"


def test_repair_is_idempotent(legacy):
    conn = db.init_db(legacy)
    auth.init_auth_schema(conn)
    before = db.columns(conn, "users")

    for _ in range(3):
        auth.init_auth_schema(conn)
    assert db.columns(conn, "users") == before


def test_asset_columns_are_healed_regardless_of_version(tmp_path):
    """The same class of bug on the assets table."""
    path = tmp_path / "assets.db"
    conn = db.init_db(path)
    auth.init_auth_schema(conn)

    # Simulate a build that shipped assets without the visibility columns.
    for index in ("idx_assets_filter", "idx_assets_gallery", "idx_assets_name",
                  "idx_assets_size", "idx_assets_vis"):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    # A trigger that reads the column would refuse the drop; such a build had none.
    conn.execute("DROP TRIGGER IF EXISTS revoke_asset_shares_on_hide")
    conn.execute("ALTER TABLE assets DROP COLUMN visibility")
    conn.commit()
    assert "visibility" not in db.columns(conn, "assets")

    assert db.heal_schema(conn) == {"assets": ["visibility"]}
    assert "visibility" in db.columns(conn, "assets")


def test_a_fresh_database_needs_no_repair(tmp_path):
    conn = db.init_db(tmp_path / "fresh.db")
    auth.init_auth_schema(conn)
    assert db.heal_schema(conn) == {}
    assert db.ensure_columns(conn, "users", auth.LATE_USER_COLUMNS) == []


def test_ensure_columns_ignores_a_table_that_is_not_there(tmp_path):
    conn = db.init_db(tmp_path / "x.db")
    assert db.ensure_columns(conn, "no_such_table", {"a": "TEXT"}) == []


def test_every_late_column_is_actually_in_the_schema(tmp_path):
    """The registry must not drift from the CREATE TABLE it mirrors."""
    conn = db.init_db(tmp_path / "check.db")
    auth.init_auth_schema(conn)

    assert set(auth.LATE_USER_COLUMNS) <= db.columns(conn, "users")
    for table, wanted in db.LATE_COLUMNS.items():
        assert set(wanted) <= db.columns(conn, table), table


def test_schema_version_is_ahead_of_the_release_that_broke(tmp_path):
    assert db.SCHEMA_VERSION >= 5


# ---------------------------------------------------------------------------
# End to end: the console page the family actually opened
# ---------------------------------------------------------------------------

def test_console_overview_loads_after_upgrading(cfg, library):
    """Boot the console against a database whose users table is from 3.0.x."""
    from conftest import ADMIN, login
    from ninaivu import build_services, create_admin_app

    # Build the database the way the old release did, then strip the columns
    # this release added — exactly the state the bug report arrived in.
    conn = db.init_db(cfg.db_path)
    auth.init_auth_schema(conn)
    for index in ("idx_users_lib",):
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    conn.execute("ALTER TABLE users DROP COLUMN library")
    conn.commit()
    assert "library" not in db.columns(conn, "users")

    cfg.watch = False
    services = build_services(cfg)          # this is where the repair happens
    services.scanner.stop()
    conn = db.connect(cfg.db_path)
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")

    client = login(create_admin_app(services).test_client(), *ADMIN)
    response = client.get("/api/admin/overview")
    assert response.status_code == 200, response.get_json()
    folders = response.get_json()["library"]["folders"]
    assert folders and folders[0]["assigned"] == 0

    # And the console can now assign one, which is what the column is for.
    listed = client.get("/api/people").get_json()["people"]
    assert listed, "the console must list the profiles it manages"

    maya = auth.create_user(conn, "maya", "summerdays24",
                            display_name="Maya", role=auth.ROLE_FAMILY)
    assigned = client.post(f"/api/people/{maya.id}",
                           json={"library": str(library / "shared")})
    assert assigned.status_code == 200, assigned.get_json()
    assert assigned.get_json()["person"]["library"] == str(library / "shared")


def test_init_db_upgrades_legacy_assets_without_is_live(tmp_path):
    """Bug: init_db failed on startup with 'no such column: is_live' on legacy database."""
    path = tmp_path / "legacy_v5.db"
    raw = sqlite3.connect(path)
    raw.execute("""
        CREATE TABLE assets (
            id          INTEGER PRIMARY KEY,
            root        TEXT NOT NULL,
            rel_path    TEXT NOT NULL,
            filename    TEXT NOT NULL,
            kind        TEXT NOT NULL
        )
    """)
    raw.commit()
    raw.close()
    db.close_all()

    conn = db.init_db(path)
    have = db.columns(conn, "assets")
    assert "is_live" in have
    assert "visibility" in have

    # Check that indexes were created successfully
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='assets'"
        )
    }
    assert "idx_assets_live" in indexes
    assert "idx_assets_vis" in indexes

