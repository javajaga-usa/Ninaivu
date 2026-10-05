"""What the household leaves out of the cloud backup.

The rule is asked three ways — over the index, over the queue, and per file as
it is about to go — and the three have to agree. And a rule only ever holds
back what has not gone: nothing already in Drive is touched by one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.cloud import store
from ninaivu.cloud.rules import REASON, Rules, clean_folders, clean_words
from ninaivu.server import auth


@pytest.fixture()
def console(scanned, library: Path):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    client = login(create_admin_app(services).test_client(), *ADMIN)
    return client, cfg, services, conn


def _states(conn):
    return {row["rel_path"]: (row["state"], row["error"])
            for row in conn.execute("SELECT rel_path, state, error FROM cloud_uploads")}


def test_no_rules_hold_nothing():
    rules = Rules()
    assert not rules.any and rules.held() == ("0", [])
    assert rules.why("2019/film.mkv", 10 ** 12, "video") is None


@pytest.mark.parametrize("rules,rel,size,kind,held", [
    (Rules(kinds="no_video"), "a/clip.mp4", 5, "video", True),
    (Rules(kinds="no_video"), "a/shot.jpg", 5, "picture", False),
    (Rules(kinds="pictures"), "a/note.mp3", 5, "audio", True),
    (Rules(kinds="pictures"), "a/odd.ts", 5, "unknown", True),
    (Rules(kinds="pictures"), "a/shot.jpg", 5, "picture", False),
    (Rules(max_mb=1), "a/big.jpg", 1024 * 1024 + 1, "picture", True),
    (Rules(max_mb=1), "a/ok.jpg", 1024 * 1024, "picture", False),
    (Rules(folders=("Downloads",)), "downloads/film.mp4", 5, "video", True),
    (Rules(folders=("Downloads",)), "Downloads2/film.mp4", 5, "video", False),
    (Rules(folders=("2014/01",)), "2014/01/16/x.jpg", 5, "picture", True),
    (Rules(folders=("2014/01",)), "2014/010/x.jpg", 5, "picture", False),
    (Rules(words=("YouTube",)), "2014/a - youtube.mp4", 5, "video", True),
    (Rules(words=("100%",)), "2014/1000.mp4", 5, "video", False),
    (Rules(words=("a_b",)), "2014/axb.mp4", 5, "video", False),
])
def test_the_sql_and_the_per_file_answer_agree(rules, rel, size, kind, held):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(rel_path TEXT, size INTEGER, kind TEXT)")
    conn.execute("INSERT INTO t VALUES(?,?,?)", (rel, size, kind))
    where, args = rules.held()
    in_sql = conn.execute(f"SELECT COUNT(*) FROM t WHERE {where}", args).fetchone()[0] == 1
    assert in_sql is held
    assert (rules.why(rel, size, kind) is not None) is held


def test_lists_are_tidied_and_cannot_climb_out():
    assert clean_folders("Downloads/\n\n ../etc \n2014\\01\nDOWNLOADS") == ["Downloads", "2014/01"]
    assert clean_words(["youtube", "x", "", "YouTube", 7]) == ["youtube"]


def test_a_rule_keeps_files_out_of_the_queue(console):
    client, cfg, services, conn = console
    cfg.cloud_skip_folders = ["shared"]
    assert services.cloud.queue_library() > 0
    queued = _states(conn)
    assert queued and not any(rel.startswith("shared/") for rel in queued)


def test_saving_a_rule_sets_aside_what_was_waiting_and_lifting_it_releases(console):
    client, cfg, services, conn = console
    services.cloud.queue_library()
    total = len(_states(conn))

    answer = client.post("/api/cloud/rules", json={"folders": ["shared"], "words": ["secret"]})
    assert answer.status_code == 200, answer.get_json()
    body = answer.get_json()
    assert body["set_aside"] == 7 and body["rules"]["on"] is True
    assert body["rules"]["held"] == 7
    for rel, (state, error) in _states(conn).items():
        kept = rel.startswith("shared/") or "secret" in rel
        assert (state == store.SKIPPED and error.startswith(REASON)) is kept, rel
    # Offering the library again does not put them back.
    assert services.cloud.queue_library() == 0
    assert body["queue"]["pending"] == total - 7
    # And it is remembered across a restart.
    import json
    assert json.loads(cfg.config_path.read_text())["cloud_skip_folders"] == ["shared"]

    lifted = client.post("/api/cloud/rules", json={"folders": [], "words": []}).get_json()
    assert lifted["released"] == 7 and lifted["rules"]["on"] is False
    assert all(state == store.PENDING for state, _ in _states(conn).values())


def test_a_rule_never_touches_what_is_already_in_drive(console):
    client, cfg, services, conn = console
    services.cloud.queue_library()
    root = cfg.active_root
    store.record_done(conn, root, "shared/holiday/beach0.jpg", remote_id="file-1")
    client.post("/api/cloud/rules", json={"folders": ["shared"]})
    assert store.state_of(conn, root, "shared/holiday/beach0.jpg") == store.DONE
    seen = client.post("/api/cloud/rules/preview", json={"folders": ["shared"]}).get_json()
    assert seen["already_sent"] == 1 and seen["held"] == 3


def test_the_preview_changes_nothing(console):
    client, cfg, services, conn = console
    services.cloud.queue_library()
    before = _states(conn)
    seen = client.post("/api/cloud/rules/preview", json={"kinds": "no_video", "max_mb": 1,
                                                         "words": ["shot"]}).get_json()
    assert seen["held"] == 7 and seen["to_go"] == len(before) - 7
    assert _states(conn) == before and cfg.cloud_kinds == "all"


def test_the_uploader_asks_again_just_before_a_file_goes(console):
    """A rule made while the queue drains holds what is still in it."""
    client, cfg, services, conn = console
    services.cloud.queue_library()
    cfg.cloud_skip_words = ["plain"]                    # set, but the queue not yet told
    row = dict(conn.execute(
        "SELECT * FROM cloud_uploads WHERE rel_path='misc/plain.png'").fetchone())
    engine = services.cloud.engine()
    assert engine._one(conn, None, row) == "skipped"    # noqa: SLF001 — no client is needed
    state, error = _states(conn)["misc/plain.png"]
    assert state == store.SKIPPED and "plain" in error


@pytest.mark.parametrize("body", [
    {"kinds": "films"}, {"max_mb": -1}, {"max_mb": "lots"}, {"max_mb": True},
    {"folders": 7}, {"max_mb": 10 ** 9}, [],
])
def test_nonsense_is_refused_and_nothing_is_saved(console, body):
    client, cfg, _, _ = console
    for path in ("/api/cloud/rules", "/api/cloud/rules/preview"):
        assert client.post(path, json=body).status_code == 400
    assert Rules.from_config(cfg) == Rules()


def test_the_family_app_has_no_rules_route(scanned):
    from ninaivu import create_home_app
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    home = login(create_home_app(services).test_client(), *ADMIN)
    assert home.post("/api/cloud/rules", json={}).status_code == 404
