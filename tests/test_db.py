"""Index layer: upserts, filters, user data preservation, search."""


import pytest

from ninaivu.storage import db


VIEWER = 1   # ids of the two throwaway profiles the fixture creates
OTHER = 2


@pytest.fixture()
def conn(tmp_path):
    from ninaivu.server import auth

    connection = db.init_db(tmp_path / "t.db")
    auth.init_auth_schema(connection)   # user_assets, folder_rules, users
    # user_assets has a real foreign key, so the viewers must exist.
    auth.create_user(connection, "viewer1", "passwordone1", role="family")
    auth.create_user(connection, "viewer2", "passwordtwo2", role="family")
    return connection


def record(rel_path, **extra):
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
        "tags": ["beach", "sunset"],
    }
    base.update(extra)
    return base


def test_upsert_then_update_preserves_user_data(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    db.set_user_asset(conn, VIEWER, asset_id, favorite=1, rating=5)

    # A rescan writes the same row again — favourites must survive it.
    db.upsert_asset(conn, record("a/1.jpg", size=2000))
    row = db.get_asset(conn, asset_id, viewer_id=VIEWER)
    assert row["favorite"] is True
    assert row["rating"] == 5
    assert row["size"] == 2000


def test_upsert_never_clobbers_an_admins_visibility_choice(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    db.set_visibility(conn, [asset_id], 2)

    db.upsert_asset(conn, record("a/1.jpg", size=4242))
    row = db.get_asset(conn, asset_id)
    assert row["visibility"] == 2, "a rescan must not un-hide media"
    assert row["size"] == 4242


def test_favourites_are_isolated_between_viewers(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    db.set_user_asset(conn, VIEWER, asset_id, favorite=1)
    assert db.get_asset(conn, asset_id, viewer_id=VIEWER)["favorite"] is True
    assert db.get_asset(conn, asset_id, viewer_id=OTHER)["favorite"] is False
    assert db.query_assets(conn, "/lib", viewer_id=OTHER, favorites=True, limit=9)[1] == 0
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, favorites=True, limit=9)[1] == 1


def test_visibility_gates_results(conn):
    db.upsert_asset(conn, record("a/pub.jpg", visibility=0))
    db.upsert_asset(conn, record("a/fam.jpg", visibility=1))
    db.upsert_asset(conn, record("a/hid.jpg", visibility=2))
    assert db.query_assets(conn, "/lib", max_visibility=0, limit=9)[1] == 1
    assert db.query_assets(conn, "/lib", max_visibility=1, limit=9)[1] == 2
    assert db.query_assets(conn, "/lib", max_visibility=2, limit=9)[1] == 3


def test_scope_confines_results_to_a_subtree(conn):
    db.upsert_asset(conn, record("holiday/1.jpg"))
    db.upsert_asset(conn, record("holiday/beach/2.jpg"))
    db.upsert_asset(conn, record("work/3.jpg"))
    assert db.query_assets(conn, "/lib", scope="holiday", limit=9)[1] == 2
    assert db.query_assets(conn, "/lib", scope="work", limit=9)[1] == 1
    assert db.query_assets(conn, "/lib", scope=None, limit=9)[1] == 3
    # A prefix that merely shares characters must not match.
    assert db.query_assets(conn, "/lib", scope="holi", limit=9)[1] == 0


def test_folder_rule_resolution_prefers_the_deepest_match(conn):
    db.set_folder_visibility(conn, "/lib", "", 0)
    db.set_folder_visibility(conn, "/lib", "private", 2)
    rules = db.folder_rules(conn, "/lib")
    assert db.visibility_for_folder(rules, "holiday") == 0
    assert db.visibility_for_folder(rules, "private") == 2
    assert db.visibility_for_folder(rules, "private/2024") == 2


def test_missing_not_null_fields_get_defaults(conn):
    asset_id = db.upsert_asset(conn, {
        "root": "/lib", "rel_path": "b/2.jpg", "filename": "2.jpg", "kind": "picture",
    })
    row = db.get_asset(conn, asset_id)
    assert row["nsfw"] is False
    assert row["nsfw_score"] == 0
    assert row["tags"] == []
    assert row["date_source"] == "mtime"
    assert row["visibility"] == 1, "new media is family-only by default"


def test_bulk_upsert(conn):
    db.bulk_upsert(conn, [record(f"a/{i}.jpg") for i in range(20)])
    _, total = db.query_assets(conn, "/lib", viewer_id=VIEWER, limit=100)
    assert total == 20


def test_delete_missing_returns_thumbs(conn, tmp_path):
    """A real folder, because pruning is a claim about one.

    `/lib` is fine for every other test here — the root is just a string to
    group rows by. Not for this one: a prune now refuses outright when the
    folder is not reachable, which is the whole point of that guard, and a
    library folder that never existed is indistinguishable from a drive
    somebody unplugged.
    """
    root = tmp_path / "lib"
    root.mkdir()
    db.bulk_upsert(conn, [record(f"a/{i}.jpg", thumb=f"ab/{i}", root=str(root))
                          for i in range(3)])
    orphans = db.delete_missing(conn, str(root), ["a/0.jpg"])
    assert sorted(orphans) == ["ab/1", "ab/2"]
    _, total = db.query_assets(conn, str(root), viewer_id=VIEWER, limit=10)
    assert total == 1


def test_a_scan_run_records_how_it_ended(conn):
    """Every row said "running", including the ones that had plainly finished.

    Which made the one row that really was still running — a scan that died, or
    one stood down for the archive and never resumed — impossible to pick out.
    """
    finished = db.start_scan_run(conn, "/lib")
    cancelled = db.start_scan_run(conn, "/lib")
    running = db.start_scan_run(conn, "/lib")

    db.finish_scan_run(conn, finished, status="done", processed=12)
    db.finish_scan_run(conn, cancelled, status="idle", processed=3)

    rows = {r["id"]: r for r in db.scan_history(conn)}
    assert rows[finished]["status"] == "done"
    assert rows[finished]["processed"] == 12
    assert rows[cancelled]["status"] == "idle"
    assert rows[running]["status"] == "running"
    assert rows[running]["ended_at"] is None


def test_a_scan_run_that_ends_without_saying_how_counts_as_done(conn):
    run = db.start_scan_run(conn, "/lib")
    db.finish_scan_run(conn, run, processed=1)
    assert db.scan_history(conn)[0]["status"] == "done"


def test_existing_signatures(conn):
    db.upsert_asset(conn, record("a/1.jpg", mtime=12.5, size=99))
    signatures = db.existing_signatures(conn, "/lib")
    assert signatures["a/1.jpg"][:2] == (12.5, 99)


def test_nsfw_hidden_unless_requested(conn):
    db.upsert_asset(conn, record("a/safe.jpg"))
    db.upsert_asset(conn, record("a/spicy.jpg", nsfw=1, nsfw_score=0.9))
    visible, total = db.query_assets(conn, "/lib", viewer_id=VIEWER, limit=10)
    assert total == 1 and visible[0]["filename"] == "safe.jpg"
    _, with_nsfw = db.query_assets(conn, "/lib", viewer_id=VIEWER, include_nsfw=True, limit=10)
    assert with_nsfw == 2


def test_filters(conn):
    db.upsert_asset(conn, record("a/1.jpg", kind="picture", date_key="2020-01-01"))
    db.upsert_asset(conn, record("b/2.mp4", kind="video", ext="mp4",
                                 date_key="2022-06-01", tags=["car"]))
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, kinds=["video"], limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, folder="b", limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, tag="car", limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, date_from="2021-01-01", limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, date_to="2021-01-01", limit=10)[1] == 1


def test_text_search_covers_filename_and_tags(conn):
    db.upsert_asset(conn, record("a/beachday.jpg", tags=["ocean"]))
    db.upsert_asset(conn, record("a/office.jpg", tags=["desk"]))
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="beach", limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="ocean", limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="zzz", limit=10)[1] == 0


def test_text_search_also_covers_the_place_a_photograph_was_taken(conn):
    """"Paris" in the search box should work the same as a tag or a caption.

    ninaivu.utils.places fills in ``city`` from GPS during a scan; before this
    it went into the index for storage and display but not for search, so
    the one word a person is most likely to type for a trip — the place's
    name — silently found nothing.
    """
    db.upsert_asset(conn, record("a/eiffel.jpg", city="Paris"))
    db.upsert_asset(conn, record("a/desk.jpg", city="Springfield"))
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="paris", limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="Paris", limit=10)[1] == 1
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="tokyo", limit=10)[1] == 0


def test_place_search_survives_an_upgrade_from_before_city_was_indexed(conn):
    """A database whose FTS index predates the ``city`` column must widen
    itself rather than search four columns forever — see :func:`db._heal_fts`.
    """
    db.upsert_asset(conn, record("a/eiffel.jpg", city="Paris"))
    conn.executescript("""
        DROP TRIGGER IF EXISTS assets_ai;
        DROP TRIGGER IF EXISTS assets_ad;
        DROP TRIGGER IF EXISTS assets_au;
        DROP TABLE IF EXISTS assets_fts;
        CREATE VIRTUAL TABLE assets_fts USING fts5(
            filename, folder, tags, caption, camera, ocr_text,
            content='assets', content_rowid='id', tokenize='unicode61'
        );
        INSERT INTO assets_fts(assets_fts) VALUES('rebuild');
    """)
    conn.commit()
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="paris", limit=10)[1] == 0

    healed = db._heal_fts(conn)
    conn.executescript(db._FTS_SCHEMA)
    if healed:
        conn.execute("INSERT INTO assets_fts(assets_fts) VALUES('rebuild')")
    conn.commit()
    assert healed, "a five-column index must be recognised as needing a rebuild"
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, text="paris", limit=10)[1] == 1


def test_search_input_cannot_break_the_query(conn):
    db.upsert_asset(conn, record("a/1.jpg"))
    for nasty in ['" OR 1=1 --', "'; DROP TABLE assets; --", "*", "NEAR(", ")"]:
        rows, total = db.query_assets(conn, "/lib", viewer_id=VIEWER, text=nasty, limit=10)
        assert isinstance(total, int)
    assert db.query_assets(conn, "/lib", viewer_id=VIEWER, limit=10)[1] == 1  # table still there


def test_sorting(conn):
    db.upsert_asset(conn, record("a/b.jpg", captured_at=100, size=10))
    db.upsert_asset(conn, record("a/a.jpg", captured_at=200, size=99))
    newest = db.query_assets(conn, "/lib", viewer_id=VIEWER, sort="date_desc", limit=10)[0]
    assert newest[0]["filename"] == "a.jpg"
    by_name = db.query_assets(conn, "/lib", viewer_id=VIEWER, sort="name_asc", limit=10)[0]
    assert by_name[0]["filename"] == "a.jpg"
    by_size = db.query_assets(conn, "/lib", viewer_id=VIEWER, sort="size_desc", limit=10)[0]
    assert by_size[0]["size"] == 99


def test_pagination_is_disjoint(conn):
    db.bulk_upsert(conn, [record(f"a/{i:02d}.jpg") for i in range(10)])
    page1, _ = db.query_assets(conn, "/lib", viewer_id=VIEWER, sort="name_asc", limit=4, offset=0)
    page2, _ = db.query_assets(conn, "/lib", viewer_id=VIEWER, sort="name_asc", limit=4, offset=4)
    assert not ({r["id"] for r in page1} & {r["id"] for r in page2})


def test_embeddings_round_trip(conn):
    asset_id = db.upsert_asset(conn, record("a/1.jpg"))
    vector = (0.5).to_bytes if False else b"\x00" * 2048
    db.store_embedding(conn, asset_id, "m", 512, vector)
    ids, blobs, dim = db.load_embeddings(conn, "/lib")
    assert ids == [asset_id] and dim == 512 and blobs[0] == vector


def test_library_stats(conn):
    db.upsert_asset(conn, record("a/1.jpg", size=100))
    db.upsert_asset(conn, record("a/2.mp4", kind="video", size=900))
    stats = db.library_stats(conn, "/lib", viewer_id=VIEWER)
    assert stats["count"] == 2 and stats["bytes"] == 1000
    assert stats["pictures"] == 1 and stats["videos"] == 1


def test_facets(conn):
    db.upsert_asset(conn, record("a/1.jpg", tags=["beach"], camera="Cam"))
    db.upsert_asset(conn, record("a/2.jpg", tags=["beach", "sun"], camera="Cam"))
    facets = db.facets(conn, "/lib")
    assert facets["tags"][0] == {"name": "beach", "count": 2}
    assert facets["cameras"][0]["count"] == 2
    assert facets["years"][0]["year"] == "2020"


def test_a_non_text_update_leaves_the_search_index_alone(tmp_path):
    """Only the indexed columns rewrite a row's full-text entry.

    Firing on every column made each flag, score and occasion write cost two
    full-text operations, and whole-library passes held the write lock for
    tens of seconds.
    """
    conn = db.init_db(tmp_path / "index.db")
    if not db.has_fts5(conn):
        pytest.skip("no FTS5 in this SQLite")
    sql = conn.execute("SELECT sql FROM sqlite_master "
                       "WHERE name='assets_au'").fetchone()[0]
    assert "UPDATE OF" in sql.upper()


def test_an_old_all_columns_trigger_is_replaced(tmp_path):
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    if not db.has_fts5(conn):
        pytest.skip("no FTS5 in this SQLite")
    conn.execute("DROP TRIGGER assets_au")
    conn.execute("CREATE TRIGGER assets_au AFTER UPDATE ON assets BEGIN SELECT 1; END")
    conn.commit()
    conn = db.init_db(path)
    sql = conn.execute("SELECT sql FROM sqlite_master "
                       "WHERE name='assets_au'").fetchone()[0]
    assert "UPDATE OF" in sql.upper()
