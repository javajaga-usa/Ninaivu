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
