"""Audit of 10 October 2026: everything a family profile can send to be kept
beside the index leaves the same 2 GB free that uploads and phone backups do."""

import io
import shutil
from collections import namedtuple

from PIL import Image

from conftest import FAMILY, login

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 400
Usage = namedtuple("Usage", "total used free")


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (120, 80, 40)).save(buf, "JPEG")
    return buf.getvalue()


def _nearly_full(monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: Usage(10**12, 10**12 - 500 * 2**20, 500 * 2**20))


def test_a_story_is_refused_when_the_disk_is_nearly_full(app, people, monkeypatch):
    fam = login(app.test_client(), *FAMILY)
    aid = fam.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    _nearly_full(monkeypatch)
    st = fam.post(f"/api/asset/{aid}/stories",
                  data={"audio": (io.BytesIO(WEBM), "s.webm", "audio/webm")},
                  content_type="multipart/form-data")
    assert st.status_code == 507
    assert "free space" in st.get_json()["error"]


def test_a_story_is_kept_when_there_is_room(app, people):
    fam = login(app.test_client(), *FAMILY)
    aid = fam.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    st = fam.post(f"/api/asset/{aid}/stories",
                  data={"audio": (io.BytesIO(WEBM), "s.webm", "audio/webm")},
                  content_type="multipart/form-data")
    assert st.status_code == 201


def test_prints_are_not_staged_when_the_disk_is_nearly_full(app, people, scanned, monkeypatch):
    cfg, _, _ = scanned
    fam = login(app.test_client(), *FAMILY)
    _nearly_full(monkeypatch)
    det = fam.post("/api/prints/detect", data={"photos": [(io.BytesIO(_jpeg()), "p.jpg")]},
                   content_type="multipart/form-data")
    assert det.status_code == 507
    from ninaivu.media import print_scan
    folder = print_scan.sessions_dir(cfg)
    assert not folder.exists() or not any(folder.iterdir())
