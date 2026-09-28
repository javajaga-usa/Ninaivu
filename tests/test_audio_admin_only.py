"""Sound recordings are administrator-only, and it is a rule, not a default.

A family media library assembled from old drives and phone backups does not
only contain photographs. It contains voice notes, voicemail, call and meeting
recordings, dictation — audio nobody chose to put in a gallery and would not
think to go and hide, because it never occurred to them it would be listed. A
photograph is looked at on purpose. A sound file plays the moment somebody
taps it, out loud, in a room with other people in it.

So audio does not appear for family members or guests, whatever its stored
visibility says, and there is no setting on the family app that lifts it. What
follows is the whole of that rule, tested from both ends: the queries that list
things, and the routes that fetch one thing by id.
"""

from __future__ import annotations

import pytest

from ninaivu.server import auth
from ninaivu.storage import db


ADMIN = auth.VIS_HIDDEN
FAMILY = auth.VIS_FAMILY
GUEST = auth.VIS_PUBLIC


@pytest.fixture()
def library(tmp_path):
    conn = db.init_db(tmp_path / "index.db")
    auth.init_auth_schema(conn)
    root = str(tmp_path / "media")
    rows = [
        _asset(root, "2019/beach.jpg", kind="picture", visibility=FAMILY),
        _asset(root, "2019/holiday.mp4", kind="video", visibility=FAMILY),
        _asset(root, "2019/voice-memo.m4a", kind="audio", visibility=FAMILY),
        _asset(root, "2019/公開.mp3", kind="audio", visibility=GUEST),
    ]
    db.bulk_upsert(conn, rows)
    return conn, root


def _asset(root, rel, *, kind, visibility):
    return {
        "root": root, "rel_path": rel, "filename": rel.rsplit("/", 1)[-1],
        "folder": rel.rsplit("/", 1)[0], "ext": "." + rel.rsplit(".", 1)[-1],
        "kind": kind, "size": 100_000, "mtime": 1.0, "date_key": "2019-07-01",
        "visibility": visibility, "vis_source": "default", "indexed_at": 1.0,
    }


def kinds_seen(conn, root, ceiling):
    rows, _ = db.query_assets(conn, root, max_visibility=ceiling, limit=100)
    return sorted({r["kind"] for r in rows})


# --- what each viewer sees -------------------------------------------------

def test_a_family_member_sees_photographs_and_video_but_no_audio(library):
    conn, root = library
    assert kinds_seen(conn, root, FAMILY) == ["picture", "video"]


def test_a_guest_sees_no_audio_either(library):
    conn, root = library
    assert "audio" not in kinds_seen(conn, root, GUEST)


def test_an_administrator_sees_everything(library):
    conn, root = library
    assert kinds_seen(conn, root, ADMIN) == ["audio", "picture", "video"]


def test_asking_for_audio_by_name_does_not_get_round_it(library):
    """The kind filter narrows what is shown; it cannot widen it."""
    conn, root = library
    rows, total = db.query_assets(conn, root, max_visibility=FAMILY,
                                  kinds=["audio"], limit=100)
    assert rows == [] and total == 0


def test_a_public_audio_file_is_still_administrator_only(library):
    """Stored visibility does not matter. This is the difference between a
    default and a rule, and it is the whole point of the change."""
    conn, root = library
    rows, _ = db.query_assets(conn, root, max_visibility=GUEST, limit=100)
    assert all(r["kind"] != "audio" for r in rows)


# --- the counts must agree with the list -----------------------------------

def test_the_sidebar_counts_do_not_mention_audio(library):
    """A count of things you cannot open is still a disclosure: it tells the
    house there are two recordings somewhere they are not being shown."""
    conn, root = library
    family = db.library_stats(conn, root, max_visibility=FAMILY)
    assert family["audio"] == 0
    assert family["count"] == 2
    assert db.library_stats(conn, root, max_visibility=ADMIN)["audio"] == 2


def test_the_totals_do_not_count_what_cannot_be_opened(library):
    conn, root = library
    _, total = db.query_assets(conn, root, max_visibility=FAMILY, limit=100)
    assert total == 2


# --- fetching one item by id ----------------------------------------------

def test_one_audio_file_by_id_is_refused_to_a_family_member(library):
    conn, root = library
    rows, _ = db.query_assets(conn, root, max_visibility=ADMIN,
                              kinds=["audio"], limit=1)
    asset = rows[0]
    assert db.visible_to(asset, FAMILY, None) is False
    assert db.visible_to(asset, GUEST, None) is False
    assert db.visible_to(asset, ADMIN, None) is True


# --- writing ---------------------------------------------------------------

def test_audio_cannot_be_set_to_family(library):
    conn, root = library
    rows, _ = db.query_assets(conn, root, max_visibility=ADMIN,
                              kinds=["audio"], limit=10)
    ids = [r["id"] for r in rows]
    assert db.set_visibility(conn, ids, FAMILY) == 0
    assert kinds_seen(conn, root, FAMILY) == ["picture", "video"]


def test_a_folder_rule_does_not_publish_the_audio_inside_it(library):
    conn, root = library
    db.set_folder_visibility(conn, root, "2019", auth.VIS_PUBLIC)
    assert kinds_seen(conn, root, GUEST) == ["picture", "video"]


def test_the_photographs_in_that_folder_did_move(library):
    """The rule is narrow: it holds audio back and touches nothing else."""
    conn, root = library
    db.set_folder_visibility(conn, root, "2019", auth.VIS_PUBLIC)
    assert kinds_seen(conn, root, GUEST) == ["picture", "video"]


def test_audio_can_still_be_hidden_explicitly(library):
    conn, root = library
    rows, _ = db.query_assets(conn, root, max_visibility=ADMIN,
                              kinds=["audio"], limit=10)
    ids = [r["id"] for r in rows]
    assert db.set_visibility(conn, ids, ADMIN) == len(ids)


# --- libraries indexed before the rule existed ----------------------------

def test_an_old_library_is_put_right_when_it_is_opened(tmp_path):
    """A database from an earlier version has audio sitting at family level.
    Opening it fixes that, rather than leaving a hole for old libraries."""
    path = tmp_path / "index.db"
    conn = db.init_db(path)
    auth.init_auth_schema(conn)
    root = str(tmp_path / "media")
    db.bulk_upsert(conn, [_asset(root, "a.m4a", kind="audio", visibility=FAMILY)])
    # Put it back the way an old version would have left it.
    conn.execute("UPDATE assets SET visibility=1, vis_source='default'")
    conn.commit()

    db.init_db(path)                      # what happens at the next start
    row = conn.execute("SELECT visibility, vis_source FROM assets").fetchone()
    assert row["visibility"] == ADMIN
    assert row["vis_source"] == "kind"


# --- and the scanner marks new ones straight away -------------------------

def test_a_newly_scanned_sound_file_arrives_administrator_only(tmp_path):
    from ninaivu.server.config import Config
    from ninaivu.media.scanner import build_record

    root = tmp_path / "media"
    root.mkdir()
    sound = root / "voicemail.m4a"
    sound.write_bytes(b"\0" * 200_000)

    cfg = Config(state_dir=tmp_path / "state", roots=[str(root)])
    cfg.ensure_dirs()
    record = build_record(root, "voicemail.m4a", sound.stat(), cfg, None)
    assert record["kind"] == "audio"
    assert record["visibility"] == ADMIN
    assert record["vis_source"] == "kind"


# --- the console's own numbers must agree ---------------------------------

def test_the_console_counts_what_a_person_would_really_see(app, people):
    """The "how much they can see" figure on a profile is what an admin checks
    before handing out a login. It has to be the number their browser would
    actually get, not a count of files in a folder."""
    from conftest import FAMILY, login

    admin = login(app.test_client(), "dad", "correcthorse1")
    family = login(app.test_client(), *FAMILY)

    # Hide a few, so the two numbers have something to disagree about.
    ids = [item["id"] for item in
           admin.get("/api/assets?limit=4").get_json()["items"]]
    assert admin.post("/api/visibility",
                      json={"ids": ids, "visibility": "hidden"}
                      ).get_json()["updated"] == len(ids)

    people_rows = admin.get("/api/people").get_json()["people"]
    maya = next(p for p in people_rows if p["username"] == FAMILY[0])
    really = family.get("/api/assets?limit=500").get_json()["total"]
    assert maya["visible_count"] == really
