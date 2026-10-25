"""Photo stacks: a burst, or a photograph and its copies, as one tile.

What a stack is (near-identical, same day, folder and who may see it), which
frame is on top, kept choices; the trigger that shows every frame again the
moment one of them is hidden, flagged or binned; then the family app: off
until switched on, the timeline collapsed only where the top photograph
stands for all of them, the frames read through the viewer's own limits,
and the household (not guests) choosing the top one or keeping them apart.
"""
from __future__ import annotations

import pytest

from ninaivu.server.config import Config
from ninaivu.server.settings_groups import GROUPS, OFF_THE_PAGE
from ninaivu.storage import db, stacks


def _row(aid, group="g", folder="f", day="2024-01-01", visibility=1, nsfw=0, private_to=None,
         sharpness=10.0, root="/lib"):
    return {"id": aid, "dup_group": group, "root": root, "folder": folder, "date_key": day,
            "visibility": visibility, "nsfw": nsfw, "private_to": private_to,
            "sharpness": sharpness, "width": 100, "height": 100, "size": 1000}


# --- what a stack is -------------------------------------------------------------------------------

def test_off_by_default_and_under_advanced():
    assert Config().photo_stacks is True                 # on by default since 3.1
    assert "photo_stacks" in GROUPS["Advanced"] and "photo_stacks" in OFF_THE_PAGE


def test_near_identical_frames_of_one_day_and_folder_are_one_stack_sharpest_on_top():
    got = stacks.plan([_row(1, sharpness=5), _row(2, sharpness=50), _row(3, sharpness=20)], {})
    key = got[1][0]
    assert key and got[2][0] == key == got[3][0]
    assert got[2] == (key, 0, 3)
    assert got[1] == (key, 1, 0) and got[3] == (key, 1, 0)


def test_a_photograph_alone_is_no_stack():
    assert stacks.plan([_row(1)], {}) == {1: (None, 0, 0)}


@pytest.mark.parametrize("change", [
    {"folder": "elsewhere"}, {"day": "2024-01-02"}, {"group": "other"}, {"root": "/other"},
    # Never a hidden or explicit frame under a family photograph, nor anybody's
    # "Only to me" item under anybody else's.
    {"visibility": 2}, {"nsfw": 1}, {"private_to": 7}, {"visibility": 0},
])
def test_frames_that_differ_in_what_matters_are_not_stacked_together(change):
    got = stacks.plan([_row(1), _row(2), _row(3, **change)], {})
    assert got[1][0] == got[2][0]
    assert got[3] == (None, 0, 0)


def test_a_chosen_top_and_a_stack_kept_apart_are_kept():
    rows = [_row(1, sharpness=99), _row(2), _row(3)]
    key = stacks.key_of(rows[0])
    assert stacks.plan(rows, {key: {"cover": 3, "apart": False}})[3] == (key, 0, 3)
    # A chosen top that left the stack: the sharpest again.
    assert stacks.plan(rows, {key: {"cover": 42, "apart": False}})[1] == (key, 0, 3)
    apart = stacks.plan(rows, {key: {"cover": None, "apart": True}})
    assert set(apart.values()) == {(key, 0, 0)}


# --- in the library --------------------------------------------------------------------------------

@pytest.fixture()
def burst(app, people):
    """The four beach photographs as one burst; a fifth photograph, shot0 and
    its copy, is a second stack of two."""
    conn = people["conn"]
    cfg = app.config["MV_CONFIG"]
    beach = [r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE filename LIKE 'beach%' ORDER BY filename")]
    assert len(beach) == 4
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("UPDATE assets SET dup_group=NULL, visibility=1, nsfw=0")
        for n, aid in enumerate(beach):
            # One second apart, in the order of their names: a stack lists
            # its frames by when they were taken.
            conn.execute("UPDATE assets SET dup_group='burst', date_key='2023-07-01', "
                         "captured_at=?, sharpness=? WHERE id=?",
                         (1688212800.0 + n, 10.0 + n * (10 if n != 2 else 50), aid))
        conn.commit()
    return {"conn": conn, "cfg": cfg, "beach": beach, "sharpest": beach[2]}


def _flags(conn):
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, stacked, stack_count FROM assets WHERE stack_key IS NOT NULL")}


def _tiles(client, query=""):
    body = client.get(f"/api/segments?{query}").get_json()
    return {item[0]: item for seg in body["segments"] for item in seg["items"]}, body["total"]


def test_the_library_is_marked_and_the_trigger_loosens_a_stack_at_once(burst):
    conn = burst["conn"]
    stacks.build(conn)
    marks = _flags(conn)
    assert marks[burst["sharpest"]] == (0, 4)
    assert sorted(v for k, v in marks.items() if k != burst["sharpest"]) == [(1, 0)] * 3
    # Hidden by hand, with the stack's marks as they were: every frame is
    # shown again before anything is built.
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (burst["beach"][0],))
        conn.commit()
    assert set(_flags(conn).values()) == {(0, 0)}
    stacks.build(conn)
    marks = _flags(conn)
    assert burst["beach"][0] not in marks
    assert marks[burst["sharpest"]] == (0, 3)
    # Binned, and gone: the same.
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("UPDATE assets SET trashed=1 WHERE id=?", (burst["beach"][1],))
        conn.commit()
    assert set(_flags(conn).values()) == {(0, 0)}
    stacks.build(conn)
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("DELETE FROM assets WHERE id=?", (burst["sharpest"],))
        conn.commit()
    assert set(_flags(conn).values()) <= {(0, 0)}


def test_a_rebuild_with_nothing_changed_writes_nothing(burst):
    conn = burst["conn"]
    assert stacks.build(conn)["changed"] == 4
    assert stacks.build(conn) == {"stacks": 1, "changed": 0}
    assert stacks.refresh(conn) is False


def test_every_route_is_404_until_switched_on(burst, as_family):
    aid = burst["beach"][0]
    assert as_family.get(f"/api/stacks/{aid}").status_code == 404
    assert as_family.post(f"/api/stacks/{aid}/top").status_code == 404
    assert as_family.post(f"/api/stacks/{aid}/apart", json={"apart": True}).status_code == 404
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["stacks"] is False
    tiles, _ = _tiles(as_family)
    assert all(burst_id in tiles for burst_id in burst["beach"])


def test_the_timeline_shows_the_top_photograph_with_its_count(burst, as_family):
    _, before = _tiles(as_family)
    burst["cfg"].photo_stacks = True
    assert as_family.get("/api/status?stats=0").get_json()["moments"]["stacks"] is True
    tiles, total = _tiles(as_family)
    assert total == before - 3
    top = tiles[burst["sharpest"]]
    assert top[3] & 1024 and top[7] == 4
    assert not any(aid in tiles for aid in burst["beach"] if aid != burst["sharpest"])
    # The folder, the day: still one tile.
    folder = as_family.get(f"/api/assets?ids={burst['sharpest']}").get_json()["items"][0]["folder"]
    tiles, _ = _tiles(as_family, f"folder={folder}")
    assert burst["sharpest"] in tiles and len([a for a in burst["beach"] if a in tiles]) == 1
    # Anything a frame under the top one may match and the top one not:
    # every frame.
    for query in ("favorites=1", "q=beach", "quality=blurry", "duplicates=1"):
        tiles, _ = _tiles(as_family, query)
        assert not any(t[3] & 1024 for t in tiles.values()), query


def test_the_viewer_reads_the_frames_through_the_viewers_limits(burst, as_family, as_admin, as_guest):
    conn, cfg = burst["conn"], burst["cfg"]
    cfg.photo_stacks = True
    got = as_family.get(f"/api/stacks/{burst['beach'][0]}").get_json()
    assert [i["id"] for i in got["items"]] == burst["beach"]
    assert got["top"] == burst["sharpest"] and got["apart"] is False and got["can_change"] is True
    item = as_family.get(f"/api/assets?ids={burst['sharpest']}").get_json()["items"][0]
    assert item["stack"] == 4
    # A hidden burst is the administrators' alone.
    with db._write_lock:                                                    # noqa: SLF001
        conn.execute("UPDATE assets SET visibility=2 WHERE dup_group='burst'")
        conn.commit()
    stacks.refresh(conn, force=True)
    assert as_family.get(f"/api/stacks/{burst['beach'][0]}").status_code == 404
    assert len(as_admin.get(f"/api/stacks/{burst['beach'][0]}").get_json()["items"]) == 4
    tiles, _ = _tiles(as_family)
    assert not any(aid in tiles for aid in burst["beach"])
    # Guests: what they may see, and no changing it.
    assert as_guest.get(f"/api/stacks/{burst['beach'][0]}").status_code == 404
    assert as_guest.post(f"/api/stacks/{burst['beach'][0]}/top").status_code == 403


def test_the_household_chooses_the_top_one_and_keeps_a_stack_apart(burst, as_family):
    burst["cfg"].photo_stacks = True
    first = burst["beach"][0]
    assert as_family.post(f"/api/stacks/{first}/top").get_json() == {"ok": True, "top": first}
    tiles, _ = _tiles(as_family)
    assert tiles[first][7] == 4 and burst["sharpest"] not in tiles
    # Kept through a rebuild.
    stacks.refresh(burst["conn"], force=True)
    assert as_family.get(f"/api/stacks/{first}").get_json()["top"] == first
    # Apart: every frame, no count; together again: the chosen top back.
    assert as_family.post(f"/api/stacks/{first}/apart", json={"apart": True}).get_json()["apart"] is True
    tiles, _ = _tiles(as_family)
    assert all(aid in tiles for aid in burst["beach"])
    assert not any(t[3] & 1024 for t in tiles.values() if t[0] in burst["beach"])
    got = as_family.get(f"/api/stacks/{first}").get_json()
    assert got["apart"] is True and got["top"] is None
    as_family.post(f"/api/stacks/{first}/apart", json={"apart": False})
    tiles, _ = _tiles(as_family)
    assert tiles[first][7] == 4


def test_a_photograph_in_no_stack_is_404(burst, as_family):
    burst["cfg"].photo_stacks = True
    stacks.build(burst["conn"])
    loner = burst["conn"].execute(
        "SELECT id FROM assets WHERE stack_key IS NULL AND kind='picture' AND trashed = 0 "
        "LIMIT 1").fetchone()[0]
    assert as_family.get(f"/api/stacks/{loner}").status_code == 404
    assert as_family.get("/api/stacks/999999").status_code == 404


def test_the_gallery_does_not_wait_behind_a_scan_for_the_marks(burst):
    """A scan holding the index's write lock: the gallery's rebuild gives up
    after a moment and writes nothing; asked by somebody's choice, it waits."""
    import threading

    conn = burst["conn"]
    held, release = threading.Event(), threading.Event()

    def scan():
        with db._write_lock:                                                # noqa: SLF001
            held.set()
            release.wait(5)

    worker = threading.Thread(target=scan)
    worker.start()
    held.wait(5)
    try:
        assert stacks.build(conn, wait=0.05) == {"stacks": 0, "changed": -1}
        assert not _flags(conn)
    finally:
        release.set()
        worker.join()
    assert stacks.build(conn, wait=0.05)["changed"] == 4
