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
