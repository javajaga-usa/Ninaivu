"""Uploads preserve existing media and respect member library boundaries."""

import io
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from ninaivu.storage import db
from ninaivu.media import upload_review


def photo(color="red"):
    content = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(content, "PNG")
    return content.getvalue()


def upload(client, content, name="photo.png"):
    return client.post("/api/upload", data={"file": (io.BytesIO(content), name)})


def test_repeated_names_preserve_every_upload(as_family, cfg):
    originals = [photo(color) for color in ("red", "green", "blue")]
    saved = []
    for content in originals:
        response = upload(as_family, content)
        assert response.status_code == 200
        result = response.get_json()
        assert result["total"] == 1, result
        saved.append(result["uploaded"][0])
    assert len({item["id"] for item in saved}) == 3
    for item, content in zip(saved, originals):
        pending = upload_review.get(db.connect(cfg.db_path), item["id"])
        assert upload_review.source_path(cfg, pending).read_bytes() == content
        assert pending["status"] == "pending"


@pytest.mark.parametrize("subfolder", ["", "trips"])
def test_upload_uses_assigned_library(as_family, people, cfg, tmp_path, subfolder):
    other = tmp_path / "other-library"
    (other / "trips").mkdir(parents=True)
    cfg.roots.append(str(other))
    assigned = other / subfolder
    people["conn"].execute("UPDATE users SET library=? WHERE id=?",
                           (str(assigned), people["family"].id))
    people["conn"].commit()
    response = upload(as_family, photo()).get_json()
    assert response["total"] == 1, response
    item = response["uploaded"][0]
    record = upload_review.get(people["conn"], item["id"])
    assert record["root"] == str(other)
    assert record["scope"] == subfolder
    assert upload_review.source_path(cfg, record).exists()
    assert not list(assigned.rglob("photo.png"))
    assert not (Path(cfg.active_root) / "Uploads" / "photo.png").exists()
    assert not any(a["filename"] == "photo.png" for a in as_family.get('/api/assets').get_json()['items'])


def test_removed_assignment_cannot_upload_elsewhere(as_family, people, tmp_path, cfg):
    people["conn"].execute("UPDATE users SET library=? WHERE id=?",
                           (str(tmp_path / "removed"), people["family"].id))
    people["conn"].commit()
    assert upload(as_family, photo()).status_code == 403
    assert not (Path(cfg.active_root) / "Uploads").exists()


def test_missing_library_is_not_recreated(as_family, cfg, tmp_path):
    missing = tmp_path / "disconnected-library"
    cfg.roots = [str(missing)]
    cfg.active_root = str(missing)
    assert upload(as_family, photo()).status_code == 409
    assert not missing.exists()


def test_failed_write_removes_partial_upload(as_family, cfg, monkeypatch):
    def interrupted(self, destination, *args, **kwargs):
        destination.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(FileStorage, "save", interrupted)
    result = upload(as_family, photo()).get_json()
    assert result["total"] == 0
    assert len(result["errors"]) == 1
    assert list((Path(cfg.state_dir) / "pending-uploads").iterdir()) == []


def test_upload_rejects_directory_link_outside_library(as_family, cfg, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (Path(cfg.state_dir) / "pending-uploads").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable")
    result = upload(as_family, photo()).get_json()
    assert result["total"] == 0 and result["errors"]
    assert list(outside.iterdir()) == []
