"""A family member's search leaves out what is nearly hidden.

Search is where the items the rules missed come to the top: a screenshot an
administrator showed to the family by hand, or a photograph the explicit check
scored just under its line. Typed words, a tag from the suggestions, the
picture search, similar photos and Ask Ninaivu leave them out for everybody
but an administrator, who still sees everything.
"""
from __future__ import annotations

import numpy as np
import pytest

from ninaivu.storage import db


class Engine:
    """A picture search that ranks every photograph the same, on SigLIP's scale."""

    semantic = True
    model_id = "test/siglip"
    search_floor = 0.0
    sigmoid_scores = True
    nsfw_threshold = 0.01
    info = {"engine": "test", "semantic": True, "model": "test/siglip"}

    def encode_text(self, query):
        return np.ones(4, dtype="float32") / 2


@pytest.fixture()
def nearly(app, people):
    """beach0 a screenshot shown to the family by hand, beach1 scored just
    under the explicit line, beach2 an ordinary photograph; all about a beach."""
    conn = people["conn"]
    ids = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM assets")}
    shown, near, plain = ids["beach0.jpg"], ids["beach1.jpg"], ids["beach2.jpg"]
    with db._write_lock:
        conn.execute("UPDATE assets SET tags='[\"beach\"]', caption='beach day'")
        conn.execute("UPDATE assets SET rel_path='shared/Screenshot_20200824-210908_WhatsApp.jpg', "
                     "visibility=1, vis_source='item' WHERE id=?", (shown,))
        conn.execute("UPDATE assets SET nsfw=0, nsfw_score=0.007 WHERE id=?", (near,))
        vector = (np.ones(4, dtype="float32") / 2).tobytes()
        for aid in ids.values():
            db.store_embedding(conn, aid, "test/siglip", 4, vector)
        conn.commit()
    cfg = app.config["MV_CONFIG"]
    cfg.nsfw_filter = True
    return {"conn": conn, "cfg": cfg, "shown": shown, "near": near, "plain": plain}


def _ids(response) -> set[int]:
    data = response.get_json()
    found = {int(i["id"]) for i in data.get("items", [])}
    for segment in data.get("segments", []):
        found |= {int(item[0]) for item in segment["items"]}
    return found


def _picture_search(app, cfg):
    engine = Engine()
    cfg.ai_enabled = True
    app.config["MV_ENGINE"] = engine
    app.config["MV_SERVICES"].engine = engine


@pytest.mark.parametrize("url", ["/api/segments?q=beach", "/api/assets?q=beach&limit=500",
                                 "/api/segments?tag=beach"])
def test_words_and_tags_leave_them_out_for_a_family_member(nearly, as_family, as_admin, app, url):
    _picture_search(app, nearly["cfg"])
    family = _ids(as_family.get(url))
    assert nearly["plain"] in family
    assert nearly["shown"] not in family and nearly["near"] not in family
    admin = _ids(as_admin.get(url))
    assert {nearly["shown"], nearly["near"], nearly["plain"]} <= admin


def test_the_picture_search_leaves_them_out(nearly, as_family, app):
    _picture_search(app, nearly["cfg"])
    answer = as_family.get("/api/segments?q=sunset")
    assert answer.get_json()["semantic"] is True
    found = _ids(answer)
    assert nearly["plain"] in found
    assert nearly["shown"] not in found and nearly["near"] not in found


def test_browsing_is_not_a_search(nearly, as_family):
    """The gallery itself keeps a file shown to the family by hand."""
    found = _ids(as_family.get("/api/assets?limit=500"))
    assert nearly["shown"] in found and nearly["near"] in found


def test_similar_photos_leave_them_out(nearly, as_family, as_admin, app):
    _picture_search(app, nearly["cfg"])
    url = f"/api/similar/{nearly['plain']}"
    family = _ids(as_family.get(url))
    assert family and nearly["shown"] not in family and nearly["near"] not in family
    assert {nearly["shown"], nearly["near"]} <= _ids(as_admin.get(url))


def test_ask_ninaivu_leaves_them_out(nearly, as_family, app):
    nearly["cfg"].ask_ninaivu = True
    _picture_search(app, nearly["cfg"])
    data = as_family.post("/api/ask-ninaivu", json={"question": "show me the beach"}).get_json()
    found = set(data["ids"])
    assert nearly["plain"] in found
    assert nearly["shown"] not in found and nearly["near"] not in found


def test_an_admins_own_verdict_stands(nearly, as_family, app):
    """Marked not explicit by hand, a near score no longer keeps it out."""
    _picture_search(app, nearly["cfg"])
    conn = nearly["conn"]
    with db._write_lock:
        conn.execute("UPDATE assets SET nsfw_source='manual' WHERE id=?", (nearly["near"],))
        conn.commit()
    assert nearly["near"] in _ids(as_family.get("/api/assets?q=beach&limit=500"))


def test_no_nearer_line_on_clips_scale(nearly, as_family, app):
    """CLIP's softmax scores sit close to its line on ordinary photographs."""
    _picture_search(app, nearly["cfg"])
    app.config["MV_ENGINE"].sigmoid_scores = False
    found = _ids(as_family.get("/api/assets?q=beach&limit=500"))
    assert nearly["near"] in found and nearly["shown"] not in found
