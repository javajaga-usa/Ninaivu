"""An album opened again is answered from the browser's cache, until it changes.

The gallery's layout carries an ETag that moves whenever the answer could.
Album views were left out, so every visit to an album worked the whole
album out again; the albums counter (a photograph joining or leaving one)
is what lets them in.
"""

from conftest import FAMILY, login


def _segment_ids(body):
    return sorted(item[0] for seg in body["segments"] for item in seg["items"])


def test_an_unchanged_album_is_a_304_and_a_changed_one_is_not(app, people):
    family = login(app.test_client(), *FAMILY)
    ids = [i["id"] for i in family.get("/api/assets?limit=5").get_json()["items"]]
    album = family.post("/api/albums", json={"name": "Trip", "ids": ids[:2]}).get_json()["id"]
    path = f"/api/segments?album={album}"

    first = family.get(path)
    tag = first.headers.get("ETag")
    assert first.status_code == 200 and tag
    assert family.get(path, headers={"If-None-Match": tag}).status_code == 304

    # A photograph joins: the old copy must not be served.
    family.post(f"/api/albums/{album}/items", json={"ids": [ids[2]]})
    changed = family.get(path, headers={"If-None-Match": tag})
    assert changed.status_code == 200
    assert _segment_ids(changed.get_json()) == sorted(ids[:3])

    # And one leaves.
    tag = changed.headers["ETag"]
    family.post(f"/api/albums/{album}/items", json={"ids": [ids[0]], "remove": True})
    again = family.get(path, headers={"If-None-Match": tag})
    assert again.status_code == 200
    assert _segment_ids(again.get_json()) == sorted(ids[1:3])


def test_two_albums_never_share_a_tag(app, people):
    family = login(app.test_client(), *FAMILY)
    ids = [i["id"] for i in family.get("/api/assets?limit=4").get_json()["items"]]
    a = family.post("/api/albums", json={"name": "A", "ids": ids[:2]}).get_json()["id"]
    b = family.post("/api/albums", json={"name": "B", "ids": ids[2:4]}).get_json()["id"]
    tag_a = family.get(f"/api/segments?album={a}").headers["ETag"]
    assert family.get(f"/api/segments?album={b}",
                      headers={"If-None-Match": tag_a}).status_code == 200


def test_the_album_query_starts_from_the_album():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "ninaivu/storage/db.py").read_text(encoding="utf-8")
    assert "a.id IN (SELECT asset_id FROM album_items WHERE album_id = ?)" in source
