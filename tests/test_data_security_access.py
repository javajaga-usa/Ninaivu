"""The data security audit of 7 October 2026: who sees what, and what stays gone.

Access between administrator, family and guest; share links; sessions; what a
deletion or a hide takes with it; and what comes back from a restore.
"""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path

import pytest
from PIL import Image

from ninaivu.storage import db, recycle


def _id(conn, name):
    return conn.execute("SELECT id FROM assets WHERE filename=?", (name,)).fetchone()["id"]


def _png() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _hide(as_admin, *ids):
    answer = as_admin.post("/api/visibility", json={"ids": list(ids), "visibility": "hidden"})
    assert answer.status_code == 200, answer.get_json()


def _pair(conn, still, clip):
    conn.execute("UPDATE assets SET kind='video', live_clip=1 WHERE id=?", (clip,))
    conn.execute("UPDATE assets SET is_live=1, live_video_path=(SELECT rel_path FROM assets "
                 "WHERE id=?) WHERE id=?", (clip, still))
    conn.commit()


# -- A live photo's clip goes with its still ------------------------------------

def test_hiding_a_live_photo_hides_its_clip(people, as_admin, as_family):
    conn = people["conn"]
    still, clip = _id(conn, "shot1.jpg"), _id(conn, "shot2.jpg")
    _pair(conn, still, clip)
    _hide(as_admin, still)
    assert as_family.get(f"/api/asset/{clip}").status_code == 404
    listed = [i["id"] for i in as_family.get("/api/assets?limit=500").get_json()["items"]]
    assert clip not in listed


def test_flagging_a_live_photo_flags_its_clip(people, as_admin):
    conn = people["conn"]
    still, clip = _id(conn, "shot1.jpg"), _id(conn, "shot2.jpg")
    _pair(conn, still, clip)
    assert as_admin.post(f"/api/asset/{still}", json={"nsfw": True}).status_code == 200
    assert conn.execute("SELECT nsfw FROM assets WHERE id=?", (clip,)).fetchone()[0] == 1


# -- Share links ----------------------------------------------------------------

def test_hiding_a_shared_photograph_ends_its_link(people, as_admin, anon):
    conn = people["conn"]
    photo = _id(conn, "shot3.jpg")
    token = as_admin.post("/api/shares", json={"scope": "asset", "target_id": photo}
                          ).get_json()["token"]
    assert anon.get(f"/api/share/{token}").status_code == 200
    _hide(as_admin, photo)
    assert anon.get(f"/api/share/{token}").status_code == 404


def test_flagging_a_shared_photograph_ends_its_link(people, as_admin, anon):
    conn = people["conn"]
    photo = _id(conn, "shot3.jpg")
    token = as_admin.post("/api/shares", json={"scope": "asset", "target_id": photo}
                          ).get_json()["token"]
    as_admin.post(f"/api/asset/{photo}", json={"nsfw": True})
    assert anon.get(f"/api/share/{token}").status_code == 404


def test_an_admin_may_still_share_a_hidden_photograph_on_purpose(people, as_admin, anon):
    conn = people["conn"]
    photo = _id(conn, "shot3.jpg")
    _hide(as_admin, photo)
    token = as_admin.post("/api/shares", json={"scope": "asset", "target_id": photo}
                          ).get_json()["token"]
    assert anon.get(f"/api/share/{token}").status_code == 200


def test_a_link_lasts_thirty_days_unless_told(people, as_family):
    conn = people["conn"]
    share = as_family.post("/api/shares", json={"scope": "asset",
                                                "target_id": _id(conn, "shot3.jpg")}
                           ).get_json()["share"]
    assert share["expires_at"] == pytest.approx(time.time() + 30 * 86400, abs=60)


def test_only_an_admin_makes_a_link_that_never_ends(people, as_family, as_admin):
    conn = people["conn"]
    photo = _id(conn, "shot3.jpg")
    family = as_family.post("/api/shares", json={"scope": "asset", "target_id": photo,
                                                 "expires_in_days": 0}).get_json()["share"]
    assert family["expires_at"] == pytest.approx(time.time() + 365 * 86400, abs=60)
    admin = as_admin.post("/api/shares", json={"scope": "asset", "target_id": photo,
                                               "expires_in_days": 0}).get_json()["share"]
    assert admin["expires_at"] is None


# -- Albums ---------------------------------------------------------------------

def test_an_album_name_is_not_a_door_into_somebody_elses_album(people, as_admin, as_family):
    conn = people["conn"]
    secret = _id(conn, "secret0.jpg")
    _hide(as_admin, secret)
    album = as_admin.post("/api/albums", json={"name": "Papers", "ids": [secret]}).get_json()["id"]
    answer = as_family.post("/api/albums", json={"name": "Papers",
                                                 "ids": [_id(conn, "shot4.jpg")]})
    assert answer.status_code == 409
    assert db.album_asset_ids(conn, album) == [secret]
    assert all(a["id"] != album for a in as_family.get("/api/albums").get_json()["albums"])


def test_a_smart_album_names_only_people_the_viewer_can_see(people, as_admin, as_family):
    conn = people["conn"]
    secret = _id(conn, "secret0.jpg")
    conn.execute("INSERT INTO people_clusters(id, name, created_at) VALUES (77, 'Someone', 0)")
    conn.execute("INSERT INTO faces(asset_id, person_id, bbox, embedding) "
                 "VALUES (?, 77, '[0,0,1,1]', x'00')", (secret,))
    conn.commit()
    _hide(as_admin, secret)
    answer = as_family.post("/api/smart-albums?person=77", json={"name": "x", "shared": False})
    assert answer.status_code in (200, 201), answer.get_json()
    assert "Someone" not in answer.get_json()["rules"]["people_names"]


# -- Occasions ------------------------------------------------------------------

def test_a_guest_is_not_told_where_an_occasion_was(people, as_guest):
    conn = people["conn"]
    for name in ("shot0.jpg", "shot0_copy.jpg"):
        conn.execute("UPDATE assets SET visibility=0 WHERE id=?", (_id(conn, name),))
    conn.execute("INSERT INTO occasions(id, root, key, title, place, started_at, ended_at, "
                 "days, count) VALUES (900, (SELECT root FROM assets LIMIT 1), 'k', "
                 "'Chennai, May 2023', 'Chennai', 0, 0, 1, 2)")
    conn.execute("UPDATE assets SET occasion_id=900 WHERE filename IN ('shot0.jpg','shot0_copy.jpg')")
    conn.commit()
    occasions = as_guest.get("/api/occasions").get_json()["occasions"]
    assert occasions and all(o["place"] == "" for o in occasions)
    assert [o["title"] for o in occasions] == ["May 2023"]


def test_hidden_photographs_do_not_shape_an_occasion(people, cfg):
    conn = people["conn"]
    conn.execute("UPDATE assets SET visibility=2")
    conn.commit()
    db.rebuild_occasions(conn, cfg.active_root, gap_seconds=6 * 3600, min_items=2)
    assert conn.execute("SELECT COUNT(*) FROM occasions").fetchone()[0] == 0


# -- Sessions and sign-in ---------------------------------------------------------

def test_putting_a_pin_on_a_profile_signs_it_out(app, people, as_admin):
    conn = people["conn"]
    family = people["family"]
    conn.execute("UPDATE users SET password=NULL WHERE id=?", (family.id,))
    conn.commit()
    browser = app.test_client()
    assert browser.post("/api/auth/enter", json={"id": family.id}).status_code == 200
    assert as_admin.post(f"/api/people/{family.id}", json={"pin": "482913"}).status_code == 200
    assert browser.get("/api/me").get_json()["id"] != family.id


def test_a_session_ends_at_its_maximum_age_however_often_used(people):
    from ninaivu.server import auth
    conn = people["conn"]
    token, _ = auth.start_session(conn, people["family"].id, "test")
    assert auth.session_user(conn, token) is not None
    conn.execute("UPDATE sessions SET created_at=? WHERE token=?",
                 (time.time() - auth.SESSION_MAX_AGE - 10, auth.session_key(token)))
    conn.commit()
    assert auth.session_user(conn, token) is None


def test_a_tap_profile_does_not_open_through_a_public_tunnel(app, cfg, people):
    conn = people["conn"]
    family = people["family"]
    conn.execute("UPDATE users SET password=NULL WHERE id=?", (family.id,))
    conn.commit()
    cfg.remote_access = "tunnel"
    cfg.remote_hostname = "photos.example.org"
    browser = app.test_client()
    answer = browser.post("/api/auth/enter", json={"id": family.id},
                          environ_base={"REMOTE_ADDR": "93.184.216.34"})
    assert answer.status_code == 403
    at_home = browser.post("/api/auth/enter", json={"id": family.id},
                           environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert at_home.status_code == 200


def test_browsing_without_signing_in_is_closed_through_a_public_tunnel(app, cfg, people):
    cfg.remote_access = "tunnel"
    cfg.remote_hostname = "photos.example.org"
    cfg.open_browsing = True
    browser = app.test_client()
    outside = browser.get("/api/assets", environ_base={"REMOTE_ADDR": "93.184.216.34"})
    assert outside.status_code == 401
    assert browser.get("/api/assets", environ_base={"REMOTE_ADDR": "192.168.1.20"}
                       ).status_code == 200


def test_a_tunnel_that_names_its_visitor_another_way_is_not_this_computer(app):
    from ninaivu.server import auth
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                                  headers={"CF-Connecting-IP": "93.184.216.34"}):
        assert not auth.request_is_local(trusted_proxies=1)
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert auth.request_is_local(trusted_proxies=1)


def test_https_is_pinned_only_for_names_with_a_public_certificate(app, cfg, people):
    cfg.remote_hostname = "photos.example.org"
    browser = app.test_client()
    public = browser.get("/healthz", base_url="https://photos.example.org")
    assert "max-age" in public.headers.get("Strict-Transport-Security", "")
    own_ca = browser.get("/healthz", base_url="https://ninaivu.local")
    assert "Strict-Transport-Security" not in own_ca.headers


# -- AI that is not on this machine ---------------------------------------------

def test_a_hidden_photograph_takes_no_generative_edit(people, as_admin):
    conn = people["conn"]
    secret = _id(conn, "secret0.jpg")
    _hide(as_admin, secret)
    answer = as_admin.post("/api/ai-playground/generate",
                           json={"prompt": "brighter", "image": _png(), "media_id": secret})
    assert answer.status_code == 403
    assert "Hidden" in answer.get_json()["error"]


def test_a_hidden_photograph_is_not_sent_to_the_ai_server(people, as_admin, monkeypatch):
    from ninaivu import extensions
    conn = people["conn"]
    secret = _id(conn, "secret0.jpg")
    _hide(as_admin, secret)
    sent = []

    class Studio:
        def job(self, cfg, kind, image, data):
            sent.append(kind)
            return lambda report: b""

    monkeypatch.setattr(extensions, "studio", lambda: Studio())
    answer = as_admin.post("/api/ai-playground/server-jobs",
                           json={"kind": "upscale", "image": _png(), "media_id": secret})
    assert answer.status_code == 403
    assert sent == []


def test_a_family_photograph_still_goes_to_the_ai_server(people, as_admin, monkeypatch):
    from ninaivu import extensions
    conn = people["conn"]
    photo = _id(conn, "shot3.jpg")
    sent = []

    class Studio:
        def job(self, cfg, kind, image, data):
            sent.append(kind)
            return lambda report: b""

    monkeypatch.setattr(extensions, "studio", lambda: Studio())
    answer = as_admin.post("/api/ai-playground/server-jobs",
                           json={"kind": "upscale", "image": _png(), "media_id": photo})
    assert answer.status_code == 202, answer.get_json()
    assert sent == ["upscale"]


def test_the_editor_names_the_photograph_it_sends():
    source = (Path(__file__).resolve().parents[1] / "ninaivu" / "static" / "js" /
              "ai-playground" / "services" / "AIPhotoService.mjs").read_text(encoding="utf-8")
    assert "media_id" in source
    for route in ("gemini/analyze", "server-jobs", "/api/ai-playground/generate",
                  "/api/ai-playground/plan"):
        assert route in source
    assert source.count("this._tag(") >= 4


# -- What a deletion takes with it ------------------------------------------------

def _delete(as_admin, *ids):
    answer = as_admin.post("/api/delete", json={"ids": list(ids), "password": "correcthorse1"})
    assert answer.status_code == 200, answer.get_json()


def test_deleting_a_live_photo_takes_its_clip_to_the_bin(people, as_admin):
    conn = people["conn"]
    still, clip = _id(conn, "shot1.jpg"), _id(conn, "shot2.jpg")
    _pair(conn, still, clip)
    _delete(as_admin, still)
    left = {r[0] for r in conn.execute("SELECT id FROM assets WHERE id IN (?, ?)", (still, clip))}
    assert left == set()


def test_erasing_takes_the_original_kept_from_before_a_rotation(people, as_admin, cfg):
    conn = people["conn"]
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE filename='shot3.jpg'").fetchone()
    kept = recycle.keep_original(row["root"], row["rel_path"])
    assert kept is not None and kept.exists()
    _delete(as_admin, row["id"])
    entry = conn.execute("SELECT id FROM recycled WHERE asset_id=?", (row["id"],)).fetchone()["id"]
    result = recycle.purge(conn, [entry])
    assert result["purged"] == 1
    assert not kept.exists()
    assert row["id"] in result["asset_ids"]


def test_erasing_removes_the_face_crops_and_remembers_the_place(people, as_admin, cfg):
    conn = people["conn"]
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE filename='shot3.jpg'").fetchone()
    crops = Path(cfg.state_dir) / "faces"
    crops.mkdir(parents=True, exist_ok=True)
    crop = crops / f"{row['id']}_0.jpg"
    crop.write_bytes(b"face")
    _delete(as_admin, row["id"])
    entry = conn.execute("SELECT id FROM recycled WHERE asset_id=?", (row["id"],)).fetchone()["id"]
    answer = as_admin.post("/api/recycle/purge", json={"ids": [entry],
                                                       "password": "correcthorse1"})
    assert answer.status_code == 200, answer.get_json()
    assert not crop.exists()
    assert recycle.Gone(conn).holds(row["root"], row["rel_path"])


def test_a_restore_from_drive_leaves_out_what_was_deleted(people, as_admin):
    from ninaivu.cloud import restore, store
    conn = people["conn"]
    keep = conn.execute("SELECT root, rel_path, size FROM assets WHERE filename='shot4.jpg'").fetchone()
    gone = conn.execute("SELECT id, root, rel_path, size FROM assets "
                        "WHERE filename='shot3.jpg'").fetchone()
    store.init_schema(conn)
    for row in (keep, gone):
        conn.execute("INSERT INTO cloud_uploads(root, rel_path, size, state, remote_id) "
                     "VALUES (?,?,?,?,?)", (row["root"], row["rel_path"], row["size"],
                                            store.DONE, "remote-" + row["rel_path"]))
    conn.commit()
    _delete(as_admin, gone["id"])
    paths = {item.rel_path for item in restore.from_record(conn)}
    assert keep["rel_path"] in paths and gone["rel_path"] not in paths


def test_a_deleted_photograph_loses_its_sidecar(people, as_admin):
    from ninaivu.storage import xmp
    conn = people["conn"]
    row = conn.execute("SELECT id, root, rel_path FROM assets WHERE filename='shot3.jpg'").fetchone()
    sidecar = xmp.sidecar_for(Path(row["root"]) / row["rel_path"])
    sidecar.write_text(f"<x {xmp.MARK}>", encoding="utf-8")
    _delete(as_admin, row["id"])
    assert not sidecar.exists()


def test_a_hidden_photograph_says_nothing_in_a_sidecar(people):
    from ninaivu.storage import xmp
    conn = people["conn"]
    photo = _id(conn, "shot3.jpg")
    conn.execute("UPDATE assets SET caption='At the clinic', caption_source='manual', "
                 "gps_lat=13.0, gps_lon=80.2, city='Chennai', visibility=2 WHERE id=?", (photo,))
    conn.commit()
    roots = [r[0] for r in conn.execute("SELECT DISTINCT root FROM assets")]
    facts = xmp.gather(conn, roots, [photo])
    assert xmp.render(facts[photo]) == ""


# -- What leaves on a drive ------------------------------------------------------

def test_a_copy_to_a_drive_leaves_out_the_recycle_bin(cfg, tmp_path):
    from ninaivu.api.drives_api import export_skips
    from ninaivu.utils import drives
    library = tmp_path / "copy-lib"
    (library / "_deleted" / "2026-10-01").mkdir(parents=True)
    (library / "_deleted" / "2026-10-01" / "passport.jpg").write_bytes(b"x")
    (library / "2026").mkdir()
    (library / "2026" / "beach.jpg").write_bytes(b"x")
    stick = tmp_path / "stick"
    stick.mkdir()
    planned = [Path(src).name for src, _, _ in
               drives.Exporter()._walk([str(library)], str(stick),
                                       export_skips(cfg, [str(library)]))]
    assert planned == ["beach.jpg"]


def test_the_status_page_on_a_drive_lists_only_that_drive(tmp_path, monkeypatch):
    from ninaivu.archive import status_kit

    class Conn:
        def execute(self, sql, *args):
            class Rows(list):
                def fetchone(self):
                    return self[0]
            return Rows([
                {"source_path": "/a/x.jpg", "destination_path": str(tmp_path / "mine" / "x.jpg"),
                 "error": "e"},
                {"source_path": "/b/y.jpg", "destination_path": str(tmp_path / "theirs" / "y.jpg"),
                 "error": "e"},
            ])

    monkeypatch.setattr(status_kit.db, "get_db", lambda: Conn())
    total, rows = status_kit._attention(str(tmp_path / "mine"))
    assert total == 1 and [r["source_path"] for r in rows] == ["/a/x.jpg"]



def test_the_offsite_storage_key_is_written_owner_only_whatever_was_there(cfg, tmp_path):
    import os
    import stat
    from ninaivu.cloud import offsite
    copy = offsite.Offsite(cfg, lambda: db.connect(cfg.db_path))
    left = Path(cfg.state_dir) / offsite.SECRET_FILE
    left = left.with_suffix(".tmp")
    left.parent.mkdir(parents=True, exist_ok=True)
    left.write_text("old")
    os.chmod(left, 0o644)
    copy.save_secret("s3cret")
    saved = Path(cfg.state_dir) / offsite.SECRET_FILE
    assert copy.secret() == "s3cret"
    if os.name != "nt":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_the_index_overwrites_what_it_deletes(cfg):
    conn = db.connect(cfg.db_path)
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] in (1, 2)
