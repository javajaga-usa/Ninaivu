"""Family members see only clean content (the household's rule, 2026-10-26).

Explicit items are kept from them by one fragment every family query carries
(``db.kind_guard``) and by the single-item check (``db.visible_to``), not only
by each listing remembering ``nsfw = 0``. Scores already at a lowered line are
flagged on the next scan, and the explicit line on SigLIP 2's scale is the
household's ``explicit_threshold``.
"""
from __future__ import annotations

from types import SimpleNamespace

from ninaivu import ai
from ninaivu.media.scanner import AI_VERSION
from ninaivu.server.config import Config
from ninaivu.storage import db


def test_the_family_guard_carries_explicit_content():
    assert "nsfw = 0" in db.kind_guard("a", 1)
    assert "a.nsfw = 0" in db.visibility_clause("a", 1)
    assert db.kind_guard("a", 2) == "", "an administrator sees everything"


def test_one_item_by_id_is_refused_when_explicit():
    item = {"visibility": 1, "kind": "picture", "nsfw": 1, "folder": ""}
    assert not db.visible_to(item, 1, None)
    assert db.visible_to(item, 2, None)
    assert db.visible_to({**item, "nsfw": 0}, 1, None)


def test_the_explicit_line_follows_the_engines_scale():
    cfg = Config()
    assert cfg.explicit_threshold == 0.005
    siglip = SimpleNamespace(sigmoid_scores=True, nsfw_threshold=0.01)
    assert ai.explicit_line(cfg, siglip) == 0.005
    clip = SimpleNamespace(sigmoid_scores=False, nsfw_threshold=None)
    assert ai.explicit_line(cfg, clip) == cfg.nsfw_threshold


def test_scores_at_a_lowered_line_are_flagged(people):
    conn = people["conn"]
    root = conn.execute("SELECT root FROM assets LIMIT 1").fetchone()[0]
    ids = [r[0] for r in conn.execute("SELECT id FROM assets WHERE kind='picture' ORDER BY id")]
    near, low, kept, hidden = ids[:4]
    with db._write_lock:
        conn.execute("UPDATE assets SET ai_version=?", (AI_VERSION,))
        conn.execute("UPDATE assets SET nsfw=0, nsfw_score=0.007 WHERE id IN (?, ?, ?)",
                     (near, kept, hidden))
        conn.execute("UPDATE assets SET nsfw=0, nsfw_score=0.002 WHERE id=?", (low,))
        conn.execute("UPDATE assets SET nsfw_source='manual' WHERE id=?", (kept,))
        conn.execute("UPDATE assets SET visibility=2 WHERE id=?", (hidden,))
        conn.commit()
    assert db.flag_explicit_scores(conn, root, 0.005) == 1
    flags = dict(conn.execute("SELECT id, nsfw FROM assets").fetchall())
    assert flags[near] == 1
    assert flags[low] == 0, "under the line"
    assert flags[kept] == 0, "an administrator's own verdict"
    assert flags[hidden] == 0, "hidden, not explicit"


def test_family_members_never_see_an_explicit_item_anywhere(people, as_family, as_admin):
    conn = people["conn"]
    target = conn.execute("SELECT id FROM assets WHERE kind='picture' ORDER BY id").fetchone()[0]
    as_admin.post(f"/api/asset/{target}", json={"nsfw": True})
    album = as_admin.post("/api/albums", json={"name": "Trip"}).get_json()
    album_id = album.get("id") or album.get("album", {}).get("id")
    as_admin.post(f"/api/albums/{album_id}/items", json={"ids": [target]})
    for url in ("/api/assets?limit=500", f"/api/assets?album={album_id}",
                f"/api/albums/{album_id}", "/api/timeline", "/api/occasions"):
        body = as_family.get(url).get_json()
        found = {i.get("id") for i in body.get("items", [])} if isinstance(body, dict) else set()
        assert target not in found, url
    for url in (f"/api/asset/{target}", f"/api/thumb/{target}", f"/api/file/{target}"):
        assert as_family.get(url).status_code == 404, url
