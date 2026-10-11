"""Audit of 11 October 2026: what landed with 1.10.6 and after it.

Live photos and RAW+JPEG pairs (#135) made one shot of two rows. These check
that the second row is served, counted, grouped and restored as part of its
photograph and never more openly than its own state allows; that the phone
inbox answers a real sync app, which sends no browser headers; and that a
share link keeps to its maker's date limit as well as the visitor's.
"""

from __future__ import annotations

import json

import pytest
from flask.testing import FlaskClient

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.media.scanner import Scanner, same_shot
from ninaivu.storage import db
from ninaivu.utils import proxies
from test_phone_inbox import FAMILY as INBOX_FAMILY, basic, photo
from test_phone_inbox import family, key  # noqa: F401 - fixtures
from test_photo_pairs import STILL_AT, add, listed, row


@pytest.fixture()
def index(scanned):
    cfg, conn, _ = scanned
    conn.execute("DELETE FROM assets")
    conn.commit()
    return cfg, conn, cfg.active_root


@pytest.fixture()
def starts(monkeypatch):
    """Conversions asked for, without running ffmpeg."""
    calls = []

    def fake_start(self, asset_id, source, duration=None, kind="video"):
        calls.append((asset_id, str(source), duration))
        return proxies.BuildState(asset_id, "building", 0, "Converting…")
    monkeypatch.setattr(proxies.ProxyStore, "start", fake_start)
    monkeypatch.setattr(proxies.ProxyStore, "ready", lambda self, a, s=None: None)
    return calls


def _clip_on_disk(cfg, rel):
    path = __import__("pathlib").Path(cfg.active_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 64)


def _live_pair(cfg, conn, root, still_vis=0, duration=3.0, **clip):
    _clip_on_disk(cfg, "pub/IMG_1.MOV")
    still = add(conn, root, "pub/IMG_1.HEIC", "picture", visibility=still_vis,
                is_live=1, live_video_path="pub/IMG_1.MOV", date_key="2022-12-20")
    film = add(conn, root, "pub/IMG_1.MOV", "video", live_clip=1, duration=duration,
               date_key="2022-12-20", visibility=clip.pop("visibility", still_vis), **clip)
    return still, film


# --- The live clip, converted or not ------------------------------------------

def test_a_clip_kept_from_the_family_is_not_played_from_its_still(client, people, index, starts):
    cfg, conn, root = index
    still, _ = _live_pair(cfg, conn, root, still_vis=1, visibility=2)
    login(client, *FAMILY)
    assert client.get(f"/api/live-video/{still}").status_code == 404
    assert client.get(f"/api/live-video/{still}?convert=1").status_code == 404
    assert not starts


def test_a_flagged_or_binned_clip_is_not_converted(client, people, index, starts):
    cfg, conn, root = index
    still, _ = _live_pair(cfg, conn, root, still_vis=1, nsfw=1)
    login(client, *FAMILY)
    assert client.get(f"/api/live-video/{still}?convert=1").status_code == 404
    conn.execute("UPDATE assets SET nsfw=0, trashed=1 WHERE kind='video'")
    conn.commit()
    assert client.get(f"/api/live-video/{still}?convert=1").status_code == 404
    assert not starts


def test_a_guest_converts_a_live_clip_but_never_a_film(client, people, index, starts):
    cfg, conn, root = index
    still, _ = _live_pair(cfg, conn, root, still_vis=0, duration=3.0)
    login(client, *GUEST)
    assert client.get(f"/api/live-video/{still}?convert=1").status_code == 202
    conn.execute("UPDATE assets SET duration=600 WHERE kind='video'")
    conn.commit()
    assert client.get(f"/api/live-video/{still}?convert=1").status_code == 403
    assert len(starts) == 1


def test_only_convert_1_converts(client, people, index, starts):
    cfg, conn, root = index
    still, _ = _live_pair(cfg, conn, root, still_vis=1)
    login(client, *FAMILY)
    client.get(f"/api/live-video/{still}?convert=0")
    assert not starts


def test_conversions_waiting_are_bounded(tmp_path, monkeypatch):
    store = proxies.ProxyStore(tmp_path)
    monkeypatch.setattr(proxies, "ffmpeg_path", lambda: "/bin/true")
    monkeypatch.setattr(proxies.ProxyStore, "_run", lambda self, *a, **k: None)
    source = tmp_path / "a.mov"
    source.write_bytes(b"x")
    states = [store.start(i, source, 3.0).state for i in range(proxies.MAX_WAITING + 3)]
    assert states.count("building") == proxies.MAX_WAITING
    assert states[-1] == "busy"


# --- RAW + JPEG ---------------------------------------------------------------

def test_a_raw_from_another_camera_or_moment_is_not_a_twin(index):
    cfg, conn, root = index
    add(conn, root, "Wedding/IMG_0042.JPG", "picture", captured_at=1.5e9,
        date_source="exif", camera="Apple iPhone 8")
    add(conn, root, "Wedding/IMG_0042.CR2", "picture", captured_at=1.6e9,
        date_source="exif", camera="Canon EOS 80D")
    Scanner(cfg)._link_raw_pairs(conn, root)
    assert row(conn, "Wedding/IMG_0042.CR2")["raw_twin"] == 0
    assert len(listed(conn, root)) == 2


def test_same_shot_reads_only_what_both_files_say():
    assert same_shot(100.0, 101.0, "Canon EOS R6", "EOS R6")
    assert same_shot(None, 5e8, "Canon EOS R6", None)
    assert not same_shot(100.0, 200.0, None, None)
    assert not same_shot(None, None, "Apple iPhone 8", "Canon EOS 80D")


def test_a_hidden_jpeg_hides_its_raw(client, people, index):
    cfg, conn, root = index
    jpeg = add(conn, root, "c/IMG_4.JPG", "picture", visibility=2)
    raw = add(conn, root, "c/IMG_4.CR2", "picture", visibility=1)
    scanner = Scanner(cfg)
    scanner._link_raw_pairs(conn, root)
    scanner._align_pairs(conn, root)
    login(client, *FAMILY)
    assert client.get(f"/api/asset/{jpeg}").status_code == 404
    assert client.get(f"/api/asset/{raw}").status_code == 404


def test_a_raw_the_viewer_may_not_have_is_not_offered(client, people, index):
    cfg, conn, root = index
    jpeg = add(conn, root, "b/IMG_3.JPG", "picture")
    add(conn, root, "b/IMG_3.CR2", "picture", nsfw=1, size=123)
    Scanner(cfg)._link_raw_pairs(conn, root)
    login(client, *FAMILY)
    assert "raw" not in client.get(f"/api/asset/{jpeg}").get_json()


def test_a_raw_and_its_jpeg_are_not_duplicates(index):
    cfg, conn, root = index
    cfg.perceptual_hash = True
    add(conn, root, "a/IMG_1.JPG", "picture", phash="ffffffffffffffff", sharpness=10.0, size=5)
    add(conn, root, "a/IMG_1.CR2", "picture", phash="ffffffffffffffff", sharpness=12.0, size=25)
    scanner = Scanner(cfg)
    scanner._link_raw_pairs(conn, root)
    scanner._link_duplicates(conn, root)
    assert row(conn, "a/IMG_1.CR2")["dup_group"] is None
    assert db.duplicate_groups(conn, [root], max_visibility=2) == []


def test_the_raw_twin_is_found_by_its_exact_path(index):
    cfg, conn, root = index
    jpeg = add(conn, root, "a/IMG_2.JPG", "picture")
    raw = add(conn, root, "a/IMG_2.CR2", "picture")
    Scanner(cfg)._link_raw_pairs(conn, root)
    plan = " ".join(str(r[-1]) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM assets WHERE root = ? AND rel_path = ? "
        "AND raw_twin = 1 AND trashed = 0", (root, "a/IMG_2.CR2")))
    assert "rel_path=?" in plan.replace(" ", "")
    assert db.raw_twin_of(conn, dict(row(conn, "a/IMG_2.JPG")))["id"] == raw
    assert jpeg


# --- Shown once, everywhere ---------------------------------------------------

def test_on_this_day_and_the_map_show_a_pair_once(index):
    cfg, conn, root = index
    for name in ("d/IMG_5.JPG", "d/IMG_5.CR2"):
        add(conn, root, name, "picture", date_key="2020-05-01", captured_at=1588334400,
            gps_lat=10.0, gps_lon=78.0)
    Scanner(cfg)._link_raw_pairs(conn, root)
    days = db.query_on_this_day(conn, [root], month=5, day=1, max_visibility=2)
    assert [i["filename"] for i in days["items"]] == ["IMG_5.JPG"]
    points = db.query_geo_points(conn, [root], max_visibility=2)
    assert [p["filename"] for p in points] == ["IMG_5.JPG"]


def test_search_by_meaning_does_not_list_a_raw_beside_its_jpeg(client, people, index, monkeypatch):
    cfg, conn, root = index
    jpeg = add(conn, root, "e/IMG_6.JPG", "picture", date_key="2020-01-01")
    raw = add(conn, root, "e/IMG_6.CR2", "picture", date_key="2020-01-01")
    Scanner(cfg)._link_raw_pairs(conn, root)
    from ninaivu.api import api as api_mod
    monkeypatch.setattr(api_mod, "_semantic_ids",
                        lambda text, roots, conn: ([raw, jpeg], {raw: .9, jpeg: .8}))
    login(client, *ADMIN)
    data = client.get("/api/segments?q=beach").get_json()
    items = [i[0] for seg in data["segments"] for i in seg["items"]]
    assert items == [jpeg]


def test_an_album_holding_a_raw_shows_its_photograph(index):
    cfg, conn, root = index
    jpeg = add(conn, root, "a/IMG_2.JPG", "picture")
    raw = add(conn, root, "a/IMG_2.CR2", "picture")
    scanner = Scanner(cfg)
    scanner._link_raw_pairs(conn, root)
    album = db.create_album(conn, "Edits")
    album_id = album if isinstance(album, int) else album["id"]
    db.album_add(conn, album_id, [raw])
    scanner._align_pairs(conn, root)
    rows, total = db.query_assets(conn, [root], max_visibility=2, album=album_id, limit=50)
    assert [r["id"] for r in rows] == [jpeg]
    albums = db.list_albums(conn, [root], max_visibility=2)
    assert [a["n"] for a in albums if a["id"] == album_id] == [total]


# --- Live pairs made by the scan ----------------------------------------------

def test_a_film_beside_a_photograph_of_its_name_stays_a_film(index):
    cfg, conn, root = index
    add(conn, root, "cam/VID_0001.JPG", "picture", is_live=1,
        live_video_path="cam/VID_0001.MP4")
    add(conn, root, "cam/VID_0001.MP4", "video", duration=1800.0)
    Scanner(cfg)._link_live_photos(conn, root)
    assert row(conn, "cam/VID_0001.MP4")["live_clip"] == 0
    assert row(conn, "cam/VID_0001.JPG")["is_live"] == 0
    assert len(listed(conn, root)) == 2


def test_a_clip_kept_apart_is_not_joined_to_a_still_elsewhere(index):
    cfg, conn, root = index
    add(conn, root, "Shared/IMG_9.HEIC", "picture", captured_at=STILL_AT, visibility=0)
    add(conn, root, "Private/IMG_9.MOV", "video", captured_at=STILL_AT, duration=3.0,
        visibility=2)
    Scanner(cfg)._link_live_photos(conn, root)
    assert row(conn, "Shared/IMG_9.HEIC")["is_live"] == 0
    assert row(conn, "Private/IMG_9.MOV")["live_clip"] == 0


def test_a_clip_dated_only_by_its_file_is_not_joined_elsewhere(index):
    cfg, conn, root = index
    add(conn, root, "2024/05/01/IMG_4321.HEIC", "picture", captured_at=STILL_AT)
    add(conn, root, "2024/05/02/IMG_4321.MOV", "video", captured_at=STILL_AT + 8.5 * 3600 + 1,
        duration=5.0, date_source="mtime")
    Scanner(cfg)._link_live_photos(conn, root)
    assert row(conn, "2024/05/02/IMG_4321.MOV")["live_clip"] == 0


def test_an_upgraded_index_is_measured_again(scanned, tmp_path):
    cfg, conn, _ = scanned
    conn.execute("ANALYZE")
    conn.execute("DROP INDEX idx_assets_gallery")
    conn.execute("CREATE INDEX idx_assets_gallery ON assets(root, id)")   # an older shape
    conn.commit()
    again = db.init_db(cfg.db_path)
    measured = {r[0] for r in again.execute("SELECT idx FROM sqlite_stat1 WHERE tbl='assets'")}
    assert "idx_assets_gallery" in measured


def test_a_new_date_takes_the_raw_along(index):
    from datetime import datetime
    from pathlib import Path
    from ninaivu.media import date_edit
    cfg, conn, root = index
    for rel in ("2024/05/01/DSC_0100.JPG", "2024/05/01/DSC_0100.NEF"):
        (Path(root) / rel).parent.mkdir(parents=True, exist_ok=True)
        (Path(root) / rel).write_bytes(b"x")
    jpeg = add(conn, root, "2024/05/01/DSC_0100.JPG", "picture")
    add(conn, root, "2024/05/01/DSC_0100.NEF", "picture")
    Scanner(cfg)._link_raw_pairs(conn, root)
    date_edit.relocate(conn, jpeg, datetime(1998, 7, 4, 12), [root])
    assert (Path(root) / "1998/07/04/DSC_0100.NEF").is_file()
    assert row(conn, "1998/07/04/DSC_0100.JPG")["raw_path"] == "1998/07/04/DSC_0100.NEF"


# --- The bin ------------------------------------------------------------------

def test_restoring_a_photograph_brings_back_its_raw(index):
    from ninaivu.storage import recycle
    cfg, conn, root = index
    for rel in ("r/IMG_1.JPG", "r/IMG_1.CR2"):
        path = __import__("pathlib").Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    jpeg = add(conn, root, "r/IMG_1.JPG", "picture")
    add(conn, root, "r/IMG_1.CR2", "picture")
    Scanner(cfg)._link_raw_pairs(conn, root)
    recycle.recycle(conn, db.with_companions(conn, [jpeg]))
    entry = conn.execute("SELECT id FROM recycled WHERE rel_path='r/IMG_1.JPG'").fetchone()["id"]
    result = recycle.restore(conn, [entry])
    assert result["restored"] == 2
    assert (__import__("pathlib").Path(root) / "r/IMG_1.CR2").exists()


# --- The phone inbox, as a sync app speaks to it -------------------------------

def test_a_sync_app_without_browser_headers_can_send(app, key):  # noqa: F811
    phone = FlaskClient(app, use_cookies=False)
    sent = phone.put("/dav/Camera/a.png", data=photo(), headers=basic(INBOX_FAMILY[0], key))
    assert sent.status_code in (201, 204)
    body = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
    listing = phone.open("/dav/", method="PROPFIND", data=body,
                         headers={**basic(INBOX_FAMILY[0], key), "Depth": "1",
                                  "Content-Type": "application/xml"})
    assert listing.status_code == 207


def test_a_form_elsewhere_still_cannot_post_to_the_app(app, people):
    browser = FlaskClient(app)
    refused = browser.post("/api/me/avatar", data={"x": "1"})
    assert refused.status_code == 403


# --- Sign out everywhere --------------------------------------------------------

def test_signing_someone_out_everywhere_ends_their_phone_keys(app, people, key):  # noqa: F811
    as_admin = login(app.test_client(), *ADMIN)
    done = as_admin.post(f"/api/people/{people['family'].id}/signout").get_json()
    assert done["phone_keys_revoked"] == 1
    phone = FlaskClient(app, use_cookies=False)
    sent = phone.put("/dav/Camera/b.png", data=photo(), headers=basic(INBOX_FAMILY[0], key))
    assert sent.status_code == 401


# --- Share links keep to their maker's dates -------------------------------------

def test_a_share_link_shows_no_more_than_its_maker_may_see(app, people, scanned):
    from ninaivu.server import date_policy
    _, conn, _ = scanned
    rows = [dict(r) for r in conn.execute(
        "SELECT id, date_key FROM assets WHERE date_key <> '' AND live_clip=0 AND raw_twin=0 "
        "ORDER BY date_key")]
    early, late = rows[0], rows[-1]
    cutoff = late["date_key"]
    assert early["date_key"] < cutoff
    db.set_meta(conn, date_policy.KEY, json.dumps(dict(
        date_policy.DEFAULTS, cutoff=cutoff, family="before", guest="all")))
    conn.commit()
    member = login(app.test_client(), *FAMILY)
    admin = login(app.test_client(), *ADMIN)
    album = member.post("/api/albums", json={"name": "Ours", "ids": [early["id"]]}).get_json()
    admin.post(f"/api/albums/{album['id']}/items", json={"ids": [late["id"]]})
    token = member.post("/api/shares", json={"scope": "album",
                                             "target_id": album["id"]}).get_json()["token"]
    visitor = app.test_client()
    shown = [i["id"] for i in visitor.get(f"/api/share/{token}").get_json()["items"]]
    assert late["id"] not in shown and early["id"] in shown
    assert visitor.get(f"/api/share/{token}/file/{late['id']}").status_code == 404
