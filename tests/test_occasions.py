"""Auto-albums: cutting a camera roll back into the occasions it came from.

The grouping is pure arithmetic over metadata, so most of this tests the
arithmetic directly. The parts that touch the database are here to prove two
things that are easy to get wrong and invisible when they break: that a
rescan does not fork an occasion in two, and that an auto-album never shows a
viewer a count of photographs they are not allowed to open.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ninaivu.media import occasions
from ninaivu.storage import db

HOUR = 3600.0
DAY = 86400.0


def shot(when, lat=None, lon=None, city=None, ident=0):
    return {"id": ident, "captured_at": when, "mtime": when,
            "gps_lat": lat, "gps_lon": lon, "city": city}


def run_of(start, count, step=60.0, **kw):
    return [shot(start + i * step, ident=i, **kw) for i in range(count)]


# ---------------------------------------------------------------------------
# Where the cuts fall
# ---------------------------------------------------------------------------

def test_a_long_silence_ends_an_occasion():
    rows = run_of(1_000_000, 4) + run_of(1_000_000 + 3 * DAY, 4)
    assert len(occasions.split(rows, gap_seconds=6 * HOUR)) == 2


def test_an_evening_stays_with_its_afternoon():
    """Five hours of quiet is a meal, not a new occasion."""
    rows = run_of(1_000_000, 4) + run_of(1_000_000 + 5 * HOUR, 4)
    assert len(occasions.split(rows, gap_seconds=6 * HOUR)) == 1


def test_crossing_a_country_ends_an_occasion_even_on_the_same_day():
    here = run_of(1_000_000, 3, lat=51.50, lon=-0.13)
    there = run_of(1_000_000 + HOUR, 3, lat=48.86, lon=2.35)
    assert len(occasions.split(here + there,
                            gap_seconds=6 * HOUR, radius_km=60.0)) == 2


def test_moving_across_town_does_not():
    here = run_of(1_000_000, 3, lat=51.50, lon=-0.13)
    nearby = run_of(1_000_000 + HOUR, 3, lat=51.52, lon=-0.10)
    assert len(occasions.split(here + nearby,
                            gap_seconds=6 * HOUR, radius_km=60.0)) == 1


def test_a_photograph_without_coordinates_never_breaks_a_run():
    """Otherwise every scanned album in the house shatters into singletons."""
    rows = (run_of(1_000_000, 2, lat=51.50, lon=-0.13)
            + run_of(1_000_000 + 120, 2)
            + run_of(1_000_000 + 240, 2, lat=51.51, lon=-0.12))
    assert len(occasions.split(rows, gap_seconds=6 * HOUR, radius_km=60.0)) == 1


def test_stray_frames_are_not_an_occasion():
    rows = run_of(1_000_000, 2) + run_of(1_000_000 + 3 * DAY, 6)
    grouped = occasions.group(rows, gap_seconds=6 * HOUR, min_items=4)
    assert [meta["count"] for meta, _ in grouped] == [6]


# ---------------------------------------------------------------------------
# What they are called
# ---------------------------------------------------------------------------

def stamp(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("start, end, expected", [
    ((2023, 5, 12), (2023, 5, 12), "12 May 2023"),
    ((2023, 5, 12), (2023, 5, 14), "12–14 May 2023"),
    ((2023, 5, 28), (2023, 6, 2), "28 May – 2 June 2023"),
    ((2023, 12, 30), (2024, 1, 2), "30 December 2023 – 2 January 2024"),
])
def test_dates_read_the_way_a_person_would_write_them(start, end, expected):
    assert occasions.date_range(stamp(*start), stamp(*end)) == expected


def test_a_place_is_named_only_when_the_whole_run_agrees():
    same = run_of(1_000_000, 4, city="Ooty")
    assert occasions.describe(same)["place"] == "Ooty"
    assert occasions.describe(same)["title"].startswith("Ooty, ")


def test_a_run_that_crossed_somewhere_is_left_unnamed():
    mixed = (run_of(1_000_000, 2, city="Ooty")
             + run_of(1_000_000 + 300, 2, city="Coonoor"))
    assert occasions.describe(mixed)["place"] is None


def test_an_occasion_keeps_its_key_when_it_grows():
    """A photograph found later must extend the album, not fork it."""
    before = run_of(1_000_000, 4)
    after = before + run_of(1_000_000 + 600, 3)
    assert occasions.describe(after)["key"] == occasions.describe(before)["key"]


# ---------------------------------------------------------------------------
# Through the database
# ---------------------------------------------------------------------------

def test_the_scan_groups_the_library(scanned):
    _, conn, _ = scanned
    rows = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE occasion_id IS NOT NULL").fetchone()
    assert rows["n"] > 0
    assert conn.execute("SELECT COUNT(*) n FROM occasions").fetchone()["n"] > 0


def test_rebuilding_is_stable(scanned):
    """Twice over the same library is the same set of occasions."""
    cfg, conn, _ = scanned
    kw = dict(gap_seconds=6 * HOUR, radius_km=0.0, min_items=2)
    db.rebuild_occasions(conn, cfg.active_root, **kw)
    first = [r["key"] for r in conn.execute(
        "SELECT key FROM occasions ORDER BY started_at").fetchall()]
    db.rebuild_occasions(conn, cfg.active_root, **kw)
    second = [r["key"] for r in conn.execute(
        "SELECT key FROM occasions ORDER BY started_at").fetchall()]
    assert first == second and first


def test_a_rescan_does_not_blank_the_grouping(scanned):
    """``occasion_id`` is derived, so the asset upsert must leave it alone."""
    from ninaivu.media.scanner import Scanner

    cfg, conn, _ = scanned
    cfg.occasion_min_items = 2
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR, min_items=2)
    before = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE occasion_id IS NOT NULL").fetchone()["n"]
    Scanner(cfg)._run(Path(cfg.active_root), full=True)
    after = conn.execute(
        "SELECT COUNT(*) n FROM assets WHERE occasion_id IS NOT NULL").fetchone()["n"]
    assert after == before > 0


def test_trashed_photographs_are_left_out(scanned):
    cfg, conn, _ = scanned
    victim = conn.execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (victim,))
    conn.commit()
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR, min_items=2)
    still = conn.execute("SELECT occasion_id FROM assets WHERE id=?",
                         (victim,)).fetchone()["occasion_id"]
    assert still is None


def test_the_cover_is_something_with_a_picture(scanned):
    """The newest item of an occasion can be a song or a video nobody could
    thumbnail; the cover was that item, and a broken image on every visit."""
    cfg, conn, _ = scanned
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR, min_items=2)
    occasion = conn.execute(
        "SELECT occasion_id, MAX(id) newest FROM assets "
        "WHERE occasion_id IS NOT NULL GROUP BY occasion_id").fetchone()
    conn.execute("UPDATE assets SET thumb=NULL WHERE id=?", (occasion["newest"],))
    conn.commit()

    listed = next(o for o in db.list_occasions(conn, cfg.active_root, max_visibility=2)
                  if o["id"] == occasion["occasion_id"])
    assert listed["cover_id"] != occasion["newest"]
    assert conn.execute("SELECT thumb FROM assets WHERE id=?",
                        (listed["cover_id"],)).fetchone()["thumb"]


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

def test_the_occasions_endpoint_lists_them(as_family, scanned):
    cfg, conn, _ = scanned
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR, min_items=2)
    listed = as_family.get("/api/occasions").get_json()["occasions"]
    assert listed
    for occasion in listed:
        assert occasion["title"]
        assert occasion["count"] > 0


def test_browsing_can_be_narrowed_to_one_occasion(as_family, scanned):
    cfg, conn, _ = scanned
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR, min_items=2)
    listed = as_family.get("/api/occasions").get_json()["occasions"]
    first = listed[0]

    items = as_family.get(
        f"/api/assets?occasion={first['id']}&limit=500").get_json()["items"]
    assert len(items) == first["count"]


def test_a_guest_is_counted_only_what_they_may_open(as_guest, as_family, scanned):
    """The stored count is the library's; the count shown is the viewer's.

    An auto-album that said "42 photographs" to somebody who may open three
    of them would be a visibility leak wearing a friendly name.
    """
    cfg, conn, _ = scanned
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR, min_items=2)

    # Publish a couple of frames out of one occasion, and nothing else.
    occasion_id = conn.execute(
        "SELECT occasion_id FROM assets WHERE occasion_id IS NOT NULL "
        "GROUP BY occasion_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()["occasion_id"]
    public = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE occasion_id=? ORDER BY id LIMIT 2",
        (occasion_id,)).fetchall()]
    conn.executemany("UPDATE assets SET visibility=0 WHERE id=?",
                     [(i,) for i in public])
    conn.commit()

    theirs = {e["id"]: e["count"]
              for e in as_guest.get("/api/occasions").get_json()["occasions"]}
    everyone = {e["id"]: e["count"]
                for e in as_family.get("/api/occasions").get_json()["occasions"]}

    assert theirs == {occasion_id: len(public)}
    assert everyone[occasion_id] > len(public)


def test_an_unknown_occasion_matches_nothing(as_family):
    items = as_family.get("/api/assets?occasion=999999&limit=200").get_json()["items"]
    assert items == []


def test_rebuilding_touches_only_what_changed(scanned):
    """A second rebuild over an unchanged library writes nothing to assets.

    Rewriting every row each scan held the write lock long enough, on a large
    library, for the cloud upload to give up with "database is locked".
    """
    cfg, conn, _ = scanned
    kw = dict(gap_seconds=6 * HOUR, radius_km=0.0, min_items=2)
    db.rebuild_occasions(conn, cfg.active_root, **kw)
    ids = {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM occasions")}
    before = conn.total_changes
    db.rebuild_occasions(conn, cfg.active_root, **kw)
    touched = conn.total_changes - before
    # Nothing at all: an occasion whose title, dates and count are as they
    # were is not written back either (twenty thousand statements under the
    # write lock on a large library).
    assert touched == 0
    assert {r["key"]: r["id"] for r in conn.execute(
        "SELECT id, key FROM occasions")} == ids


def test_an_occasion_that_is_gone_releases_its_photographs(scanned):
    cfg, conn, _ = scanned
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR, min_items=2)
    assert conn.execute("SELECT COUNT(*) n FROM occasions").fetchone()["n"] > 0
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * HOUR,
                         min_items=10_000)
    assert conn.execute("SELECT COUNT(*) n FROM occasions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM assets "
                        "WHERE occasion_id IS NOT NULL").fetchone()["n"] == 0
