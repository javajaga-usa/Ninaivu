"""Sixth audit, backend security sweep (S-xx findings)."""

from __future__ import annotations

import io

import pytest


# -- S-01: a DASH manifest dressed as a video ---------------------------------

_MPD = (b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" '
        b'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" type="static">'
        b'<Period><AdaptationSet><Representation id="1" bandwidth="1">'
        b'<BaseURL>/srv/photos/private/hidden.mp4</BaseURL>'
        b'</Representation></AdaptationSet></Period></MPD>')


@pytest.mark.parametrize("content", [
    _MPD,
    b"\n  <!-- a phone's export -->\n" + _MPD,
    b'<CompositionPlaylist xmlns="http://www.smpte-ra.org/schemas/2067-3/2016">'
    b"<ContentTitle>x</ContentTitle></CompositionPlaylist>",
])
def test_a_dash_or_imf_manifest_dressed_as_a_video_is_refused(as_family, content):
    # ffmpeg reads "<MPD" with no <?xml line as DASH and follows its BaseURL
    # to any file on disk under -protocol_whitelist file: the thumbnail and
    # playable copy of the "upload" were then a hidden video's.
    answer = as_family.post("/api/upload", data={"file": (io.BytesIO(content), "IMG_1.mp4")})
    body = answer.get_json()
    assert not body.get("uploaded"), body
    assert "not recognised" in body["errors"][0]["error"]


# -- S-02: the weekly digest and the family's date limit -----------------------

def test_the_digest_keeps_to_the_familys_date_limit(tmp_path):
    # Emailed to the family on a schedule, with no request to take a date
    # limit from: under "family sees before 2020", a photograph from 2021 the
    # family cannot open in Ninaivu was sent to them.
    import json

    from ninaivu.server import date_policy
    from ninaivu.storage import db

    conn = db.init_db(tmp_path / "index.db")
    for year in (2018, 2021):
        conn.execute(
            "INSERT INTO assets(root, rel_path, filename, kind, date_key, size, thumb, "
            "visibility, sharpness, brightness, width, height, indexed_at) "
            "VALUES('/library', ?, ?, 'picture', ?, 0, 'ab/cdef', 1, 500, 0.5, 4000, 3000, 0)",
            (f"{year}/a.jpg", f"from-{year}.jpg", f"{year}-07-04"))
    conn.commit()

    def pool():
        return sorted(i["filename"] for i in
                      db.candidates_for_the_day(conn, ["/library"], month=7, day=4))

    assert pool() == ["from-2018.jpg", "from-2021.jpg"]
    db.set_meta(conn, date_policy.KEY, json.dumps(
        dict(date_policy.DEFAULTS, cutoff="2020-01-01", family="before")))
    assert pool() == ["from-2018.jpg"]


# -- S-03: a ZIP download names an entry by a backslash path -------------------

@pytest.mark.skipif(__import__("os").name == "nt", reason="a backslash cannot be in a name here")
def test_a_zip_download_never_carries_a_backslash_path(as_family, scanned):
    # A name like ..\..\x.jpg is one ordinary file on Linux or a NAS, but in
    # a ZIP the family member opens on Windows its backslashes are folders:
    # extracting it wrote outside the folder chosen.
    import zipfile
    from pathlib import Path

    cfg, conn, _ = scanned
    root = Path(cfg.active_root)
    evil = "misc/..\\..\\Startup\\evil.png"
    (root / "misc" / "plain.png").rename(root / evil)
    conn.execute("UPDATE assets SET rel_path=?, filename=? WHERE rel_path='misc/plain.png'",
                 (evil, evil.split("/")[-1]))
    conn.commit()
    items = as_family.get("/api/assets?limit=200").get_json()["items"]
    target = [i["id"] for i in items if "evil" in (i.get("filename") or i.get("name") or "")]
    assert target, "the planted file was not indexed"
    answer = as_family.get(f"/api/download/zip?ids={target[0]}")
    assert answer.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(answer.get_data())).namelist()
    assert names and not any("\\" in n or "/" in n or ".." in n for n in names), names


# -- S-04: a non-ASCII stop token ---------------------------------------------

def test_a_stop_token_in_another_script_is_a_404_not_a_500(app):
    # hmac.compare_digest refuses str with non-ASCII in it, so the route that
    # answers 404 to every wrong token answered this one with a 500 and a
    # traceback in the log.
    from ninaivu import create_admin_app

    admin = create_admin_app(app.config["MV_SERVICES"])
    admin.config["MV_STOP_TOKEN"] = "the-right-token"
    admin.config["MV_SHUTDOWN"] = lambda: pytest.fail("a wrong token stopped it")
    response = admin.test_client().post("/api/admin/shutdown",
                                        json={"token": "thé-right-token"})
    assert response.status_code == 404


# -- S-05: JSON nested past Python's recursion limit -----------------------------

def test_json_nested_too_deep_is_a_400_not_a_500(anon):
    # json.loads raises RecursionError, not ValueError, for "[" nested deeply:
    # anybody could answer the sign-in route with a 500 and a logged traceback.
    deep = "[" * 100000 + "]" * 100000
    response = anon.post("/api/auth/login", data=deep, content_type="application/json")
    assert response.status_code == 400


# -- S-06: two console routes answered a wrong type with a 500 ------------------

@pytest.mark.parametrize("path, body", [
    ("/api/admin/large-files/keep", {"ids": [2 ** 70]}),
    ("/api/archive/adopt", {"path": True}),
])
def test_console_routes_refuse_a_wrong_type_with_a_400(as_admin, path, body):
    assert as_admin.post(path, json=body).status_code == 400
