"""Voice stories: told about a photograph, heard only by who may see it.

A story is part of its item. These pin that down from every side a story can
be reached: listing, playing, telling, deleting and searching, for each role,
for an administrator-only item and for a family member kept to one folder —
and that the sound goes when its row goes, however the row goes.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from conftest import login
from ninaivu.server import auth
from ninaivu.storage import db, recycle, stories

#: The first bytes of the containers a browser records into, padded out.
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 400
MP4 = b"\x00\x00\x00\x18ftypM4A \x00\x00\x02\x00" + b"\x00" * 400
OGG = b"OggS" + b"\x00" * 400


def first_id(client, query="limit=1"):
    return client.get(f"/api/assets?{query}").get_json()["items"][0]["id"]


def tell(client, asset_id, *, data=WEBM, mime="audio/webm;codecs=opus", name="story.webm",
         text="", speaker="", duration="12.5"):
    form = {"audio": (io.BytesIO(data), name, mime), "duration": duration}
    if text:
        form["text"] = text
    if speaker:
        form["speaker"] = speaker
    return client.post(f"/api/asset/{asset_id}/stories", data=form,
                       content_type="multipart/form-data")


def folder(scanned) -> Path:
    cfg, _, _ = scanned
    return stories.folder_for(cfg.state_dir)


def hide(conn, asset_id, visibility=auth.VIS_HIDDEN):
    conn.execute("UPDATE assets SET visibility=? WHERE id=?", (visibility, asset_id))
    conn.commit()


# --- telling one -------------------------------------------------------------

def test_a_family_member_tells_a_story_and_it_is_kept_beside_the_index(as_family, scanned):
    asset_id = first_id(as_family)
    answer = tell(as_family, asset_id, text="This is my father's shop in 1968")
    assert answer.status_code == 201, answer.get_json()
    story = answer.get_json()["story"]
    assert story["speaker"] == "Maya", "the teller's own name when none is given"
    assert story["text"] == "This is my father's shop in 1968"
    assert story["duration"] == 12.5
    assert story["mine"] and story["can_delete"]
    files = list(folder(scanned).iterdir())
    assert len(files) == 1 and files[0].suffix == ".webm"
    cfg, _, _ = scanned
    for root in cfg.roots:
        assert not list(Path(root).rglob("*.webm")), "never in the photo library"

    listed = as_family.get(f"/api/asset/{asset_id}/stories").get_json()
    assert [s["id"] for s in listed["stories"]] == [story["id"]]
    assert listed["can_add"] is True
    assert as_family.get(f"/api/asset/{asset_id}").get_json()["stories"] == 1


def test_several_stories_with_a_chosen_speaker_and_an_iphone_recording(as_family):
    asset_id = first_id(as_family)
    assert tell(as_family, asset_id, speaker="Paati", text="அப்பாவின் கடை").status_code == 201
    answer = tell(as_family, asset_id, data=MP4, mime="audio/mp4", name="voice.mp4")
    assert answer.status_code == 201
    assert answer.get_json()["story"]["mime"] == "audio/mp4"
    listed = as_family.get(f"/api/asset/{asset_id}/stories").get_json()["stories"]
    assert [s["speaker"] for s in listed] == ["Paati", "Maya"]
    assert listed[0]["text"] == "அப்பாவின் கடை"


def test_the_sound_is_played_with_its_own_type_and_can_be_sought_in(as_family):
    asset_id = first_id(as_family)
    story = tell(as_family, asset_id, data=OGG, mime="audio/ogg").get_json()["story"]
    whole = as_family.get(story["src"])
    assert whole.status_code == 200
    assert whole.mimetype == "audio/ogg"
    assert whole.headers["X-Content-Type-Options"] == "nosniff"
    assert whole.data == OGG
    part = as_family.get(story["src"], headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.data == b"OggS"


@pytest.mark.parametrize("data, mime, name, status", [
    (b"<html><script>alert(1)</script></html>", "audio/webm", "x.webm", 415),
    (WEBM, "text/html", "x.html", 415),
    (WEBM, "image/jpeg", "x.jpg", 415),
    (WEBM, "application/octet-stream", "x.exe", 415),
    (b"", "audio/webm", "x.webm", 400),
])
def test_only_sound_is_accepted(as_family, scanned, data, mime, name, status):
    asset_id = first_id(as_family)
    answer = tell(as_family, asset_id, data=data, mime=mime, name=name)
    assert answer.status_code == status, answer.get_json()
    assert not folder(scanned).exists() or not list(folder(scanned).iterdir())


def test_a_phone_that_names_no_type_is_believed_by_the_file_name(as_family):
    answer = tell(as_family, first_id(as_family), data=MP4,
                  mime="application/octet-stream", name="Memo.m4a")
    assert answer.status_code == 201
    assert answer.get_json()["story"]["mime"] == "audio/mp4"


def test_a_story_is_at_most_25_mb(as_family, scanned):
    big = WEBM + b"\x00" * stories.MAX_BYTES
    assert tell(as_family, first_id(as_family), data=big).status_code == 413
    assert not folder(scanned).exists() or not list(folder(scanned).iterdir())


def test_the_words_and_the_name_have_limits(as_family):
    asset_id = first_id(as_family)
    assert tell(as_family, asset_id, text="x" * (stories.MAX_TEXT + 1)).status_code == 400
    assert tell(as_family, asset_id, speaker="x" * (stories.MAX_SPEAKER + 1)).status_code == 400


def test_a_recording_is_sent_as_audio(as_family):
    asset_id = first_id(as_family)
    answer = as_family.post(f"/api/asset/{asset_id}/stories", data={"text": "hello"},
                            content_type="multipart/form-data")
    assert answer.status_code == 400


# --- who may hear, tell and delete -----------------------------------------

def test_guests_may_listen_to_what_they_may_see_but_not_tell(as_family, as_guest, people, anon):
    conn = people["conn"]
    asset_id = first_id(as_family)
    story = tell(as_family, asset_id).get_json()["story"]
    # Family-only: a guest gets what they would for a photograph that is not there.
    for client in (as_guest, anon):
        assert client.get(f"/api/asset/{asset_id}/stories").status_code == 404
        assert client.get(story["src"]).status_code == 404
    hide(conn, asset_id, auth.VIS_PUBLIC)
    listed = as_guest.get(f"/api/asset/{asset_id}/stories").get_json()
    assert len(listed["stories"]) == 1 and listed["can_add"] is False
    assert not listed["stories"][0]["can_delete"]
    assert as_guest.get(story["src"]).status_code == 200
    assert tell(as_guest, asset_id).status_code == 403
    assert tell(anon, asset_id).status_code == 401
    assert as_guest.delete(f"/api/stories/{story['id']}").status_code == 403


def test_an_admin_only_item_does_not_admit_to_having_a_story(as_admin, as_family, people):
    conn = people["conn"]
    asset_id = first_id(as_family)
    story = tell(as_admin, asset_id, text="secret words").get_json()["story"]
    hide(conn, asset_id)
    assert as_family.get(f"/api/asset/{asset_id}/stories").status_code == 404
    assert as_family.get(story["src"]).status_code == 404
    assert as_family.delete(f"/api/stories/{story['id']}").status_code == 404
    assert tell(as_family, asset_id).status_code == 404
    # The same answer as a story that was never told.
    assert as_family.get("/api/stories/987654/audio").status_code == 404
    assert as_admin.get(f"/api/asset/{asset_id}/stories").status_code == 200
    assert as_admin.get(story["src"]).status_code == 200


def test_a_family_member_kept_to_one_folder_hears_only_its_stories(as_admin, as_family, people):
    conn = people["conn"]
    inside = conn.execute("SELECT id FROM assets WHERE folder='shared/holiday' LIMIT 1").fetchone()[0]
    outside = conn.execute("SELECT id FROM assets WHERE folder='private' LIMIT 1").fetchone()[0]
    told_in = tell(as_admin, inside, text="beach day").get_json()["story"]
    told_out = tell(as_admin, outside, text="beach night").get_json()["story"]
    auth.update_profile(conn, people["family"].id, scope="shared")
    assert as_family.get(f"/api/asset/{inside}/stories").status_code == 200
    assert as_family.get(told_in["src"]).status_code == 200
    assert as_family.get(f"/api/asset/{outside}/stories").status_code == 404
    assert as_family.get(told_out["src"]).status_code == 404
    found = {i["id"] for i in as_family.get("/api/assets?q=beach&limit=50").get_json()["items"]}
    assert inside in found and outside not in found


def test_only_the_teller_or_an_admin_may_delete(app, as_admin, as_family, people):
    conn = people["conn"]
    auth.create_user(conn, "arjun", "summerdays25", display_name="Arjun",
                     role=auth.ROLE_FAMILY, created_by=people["admin"].id)
    cousin = login(app.test_client(), "arjun", "summerdays25")
    asset_id = first_id(as_family)
    mine = tell(as_family, asset_id).get_json()["story"]
    theirs = cousin.get(f"/api/asset/{asset_id}/stories").get_json()["stories"][0]
    assert theirs["can_delete"] is False and theirs["mine"] is False
    assert cousin.delete(f"/api/stories/{mine['id']}").status_code == 403
    assert as_admin.get(f"/api/asset/{asset_id}/stories").get_json()["stories"][0]["can_delete"]
    assert as_family.delete(f"/api/stories/{mine['id']}").status_code == 200
    other = tell(cousin, asset_id).get_json()["story"]
    assert as_admin.delete(f"/api/stories/{other['id']}").status_code == 200
    assert as_family.get(f"/api/asset/{asset_id}/stories").get_json()["stories"] == []


# --- the sound goes with its row -------------------------------------------

def test_deleting_a_story_deletes_its_sound(as_family, scanned):
    story = tell(as_family, first_id(as_family)).get_json()["story"]
    assert len(list(folder(scanned).iterdir())) == 1
    assert as_family.delete(f"/api/stories/{story['id']}").status_code == 200
    assert list(folder(scanned).iterdir()) == []
    assert as_family.get(story["src"]).status_code == 404


def test_an_asset_deleted_by_plain_sql_takes_its_stories_and_their_sound(as_family, scanned):
    _, conn, _ = scanned
    asset_id = first_id(as_family)
    tell(as_family, asset_id)
    tell(as_family, asset_id)
    conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
    assert stories.sweep(conn) == 2
    assert list(folder(scanned).iterdir()) == []


def test_the_rows_go_even_where_foreign_keys_are_off(as_family, scanned):
    """A tool, or a migration that turned the keys off for a moment."""
    import sqlite3
    cfg, conn, _ = scanned
    asset_id = first_id(as_family)
    tell(as_family, asset_id)
    raw = sqlite3.connect(str(cfg.db_path))
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    raw.commit()
    raw.close()
    assert conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
    stories.sweep(conn)
    assert list(folder(scanned).iterdir()) == []


def test_a_story_comes_back_from_the_bin_with_its_photograph(as_family, scanned):
    _, conn, _ = scanned
    asset_id = first_id(as_family)
    tell(as_family, asset_id, text="the old shop")
    result = recycle.recycle(conn, [asset_id])
    assert result["deleted"] == 1
    stories.sweep(conn)
    assert len(list(folder(scanned).iterdir())) == 1, "kept while the bin holds it"
    entry = conn.execute("SELECT id FROM recycled").fetchone()[0]
    assert recycle.restore(conn, [entry])["restored"] == 1
    back = conn.execute("SELECT * FROM stories").fetchall()
    assert len(back) == 1 and back[0]["text"] == "the old shop"
    stories.sweep(conn)
    assert (folder(scanned) / back[0]["file"]).is_file()


def test_erasing_from_the_bin_erases_the_sound(as_family, scanned):
    _, conn, _ = scanned
    tell(as_family, first_id(as_family))
    recycle.recycle(conn, [first_id(as_family)])
    entry = conn.execute("SELECT id FROM recycled").fetchone()[0]
    assert recycle.purge(conn, [entry])["purged"] == 1
    assert list(folder(scanned).iterdir()) == []


# --- search ----------------------------------------------------------------

def test_the_words_are_found_by_the_gallery_search(as_family):
    asset_id = first_id(as_family)
    tell(as_family, asset_id, text="This is my father's shop in 1968", speaker="Paati")
    for words in ("father shop", "FATHER", "paati", "shop"):
        found = [i["id"] for i in as_family.get(f"/api/assets?q={words}&plain=1").get_json()["items"]]
        assert found == [asset_id], (words, found)
    assert as_family.get("/api/assets?q=mother+shop&plain=1").get_json()["items"] == []


def test_tamil_words_are_found(as_family):
    asset_id = first_id(as_family)
    tell(as_family, asset_id, text="இது என் அப்பாவின் கடை")
    found = [i["id"] for i in as_family.get("/api/assets?q=அப்பாவின்&plain=1").get_json()["items"]]
    assert found == [asset_id]


def test_search_finds_a_story_only_for_who_may_see_its_item(as_admin, as_family, as_guest, people):
    conn = people["conn"]
    hidden = first_id(as_family)
    tell(as_admin, hidden, text="grandfather mill")
    hide(conn, hidden)
    assert as_admin.get("/api/assets?q=grandfather&plain=1").get_json()["total"] == 1
    assert as_family.get("/api/assets?q=grandfather&plain=1").get_json()["total"] == 0
    assert as_guest.get("/api/assets?q=grandfather&plain=1").get_json()["total"] == 0
    segments = as_family.get("/api/segments?q=grandfather&plain=1").get_json()["segments"]
    assert [row[0] for seg in segments for row in seg["items"]] == []


def test_a_story_search_still_finds_ordinary_matches(as_family):
    """The full-text half keeps working beside the story half."""
    asset_id = first_id(as_family)
    tell(as_family, asset_id, text="beach holiday")
    found = {i["id"] for i in as_family.get("/api/assets?q=beach&plain=1&limit=50").get_json()["items"]}
    # The holiday folder's files are named beach0..3; the story's item joins them.
    assert asset_id in found and len(found) >= 5


# --- share links -----------------------------------------------------------

def test_a_share_link_plays_its_items_stories_and_no_others(as_family, people, app):
    conn = people["conn"]
    shared, other = (r[0] for r in conn.execute(
        "SELECT id FROM assets WHERE folder='shared/holiday' ORDER BY id LIMIT 2"))
    told = tell(as_family, shared, text="our trip", speaker="Paati").get_json()["story"]
    elsewhere = tell(as_family, other, text="not shared").get_json()["story"]
    made = as_family.post("/api/shares", json={"scope": "asset", "target_id": shared})
    token = made.get_json().get("token") or made.get_json()["share"]["token"]
    stranger = app.test_client()
    page = stranger.get(f"/api/share/{token}").get_json()
    assert page["item"]["stories"] == 1
    listed = stranger.get(f"/api/share/{token}/stories/{shared}").get_json()["stories"]
    assert [s["text"] for s in listed] == ["our trip"]
    assert "can_delete" in listed[0] and not listed[0]["can_delete"]
    assert stranger.get(listed[0]["src"]).status_code == 200
    assert stranger.get(f"/api/share/{token}/stories/{other}").status_code == 404
    assert stranger.get(f"/api/share/{token}/story/{elsewhere['id']}").status_code == 404
    assert stranger.get(f"/api/stories/{told['id']}/audio").status_code == 404
    # Hidden afterwards: the link stops showing it, stories and all.
    hide(conn, shared)
    assert stranger.get(f"/api/share/{token}/story/{told['id']}").status_code == 404


# --- what leaves the house, and what is backed up --------------------------

def test_the_drive_copy_of_the_index_carries_no_story(as_family, scanned, tmp_path):
    import tarfile
    import sqlite3
    from ninaivu.cloud import index_copy
    cfg, _, _ = scanned
    tell(as_family, first_id(as_family), text="private words")
    bundle = index_copy.make_bundle(cfg.state_dir, tmp_path / "out")
    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        assert not any("stories/" in n for n in names)
        tar.extract(next(n for n in names if n.endswith("index.db")), tmp_path / "x")
    index = next((tmp_path / "x").rglob("index.db"))
    copied = sqlite3.connect(str(index))
    assert copied.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 0
    copied.close()
    assert b"private words" not in index.read_bytes()


def test_a_local_backup_carries_the_recordings(as_family, scanned, tmp_path):
    import tarfile
    from ninaivu.storage import backup
    cfg, _, _ = scanned
    tell(as_family, first_id(as_family))
    bundle = backup.snapshot(cfg.state_dir, tmp_path / "bundles")
    with tarfile.open(bundle) as tar:
        assert any(n.startswith("state/stories/") and n.endswith(".webm") for n in tar.getnames())


def test_nothing_about_stories_reaches_an_outside_ai(as_family):
    """No route of this feature names a cloud service; no speech-to-text."""
    source = (Path(stories.__file__).read_text(encoding="utf-8")
              + Path(stories.__file__).parent.parent.joinpath("api", "api_stories.py")
              .read_text(encoding="utf-8"))
    for word in ("gemini", "requests", "urllib", "http.client", "openai", "whisper"):
        assert word not in source.lower().replace("outside service", "")


def test_a_database_without_stories_sweeps_quietly(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    assert stories.sweep(conn) == 0
    assert stories.matching_asset_ids(conn, "x") == []


def test_sniffing_knows_the_recorders(tmp_path):
    assert stories.sniff(WEBM) == "audio/webm"
    assert stories.sniff(MP4) == "audio/mp4"
    assert stories.sniff(OGG) == "audio/ogg"
    assert stories.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio/wav"
    assert stories.sniff(b"ID3\x04") == "audio/mpeg"
    assert stories.sniff(b"\xff\xfb\x90\x00") == "audio/mpeg"
    assert stories.sniff(b"\xff\xf1\x50\x80") == "audio/aac"
    assert stories.sniff(b"<!doctype html>") is None
    assert stories.declared_type("audio/webm;codecs=opus") == "audio/webm"
    assert stories.declared_type("audio/x-m4a") == "audio/mp4"
    assert stories.declared_type("video/webm") is None
    assert stories.declared_type("", "a.ogg") == "audio/ogg"


def test_db_unaffected_without_story_words(scanned):
    """A search no story answers is ranked as before."""
    cfg, conn, _ = scanned
    rows, total = db.query_assets(conn, cfg.roots, text="beach", max_visibility=2)
    assert total == 4 and all("beach" in r["filename"] for r in rows)



def test_only_the_family_page_may_ask_for_the_microphone(client, as_family):
    """The family app records; nothing else Ninaivu serves may even ask."""
    page = client.get("/").headers["Permissions-Policy"]
    assert "microphone=(self)" in page and "camera=()" in page
    for path in ("/api/health", "/api/assets?limit=1"):
        assert "microphone=()" in as_family.get(path).headers["Permissions-Policy"]


def test_the_console_page_may_not_ask_for_the_microphone(scanned):
    from ninaivu import build_services, create_admin_app
    cfg, _, _ = scanned
    cfg.watch = False
    services = build_services(cfg)
    services.scanner.stop()
    try:
        console = create_admin_app(services).test_client()
        assert "microphone=()" in console.get("/").headers["Permissions-Policy"]
    finally:
        services.stop(timeout=5.0)
