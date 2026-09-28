"""The capacity estimate, and the selection it is supposed to reflect.

An estimate reading "56,945 files, 1.4 TB" is unfalsifiable: there is no way to
tell from it that the job is quietly including video the user thought they had
excluded. These tests pin down both halves of that fault — the selection being
honoured, and the estimate saying enough for a wrong one to be visible.
"""

import pytest
from PIL import Image

from conftest import ADMIN, login
from ninaivu.server import auth
from ninaivu import build_services, create_admin_app

MB = 1024 * 1024


@pytest.fixture()
def console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    app = create_admin_app(services)
    return login(app.test_client(), *ADMIN), cfg, services


@pytest.fixture()
def mixed(tmp_path):
    """A folder holding small photos, big video and one audio file."""
    src = tmp_path / "Shoebox"
    src.mkdir()
    for i in range(4):
        Image.new("RGB", (64, 48), (i * 40, 90, 30)).save(src / f"pic{i}.jpg")
    for i in range(3):
        (src / f"clip{i}.mp4").write_bytes(b"\0" * (2 * MB))
    (src / "song.mp3").write_bytes(b"\0" * MB)
    return src


def estimate(client, source, types, dest, **extra):
    body = {"source_dirs": [{"path": str(source), "types": list(types)}],
            "destination_dir": str(dest), "deep_scan": True, **extra}
    result = client.post("/api/archive/capacity", json=body).get_json()
    assert result["ok"], result
    return result


# -- the selection is honoured ---------------------------------------------

def test_photos_only_counts_photos_only(console, mixed, tmp_path):
    client, _, _ = console
    result = estimate(client, mixed, ["image"], tmp_path / "Master")
    assert result["files"] == 4
    assert result["bytes"] < MB, "video was counted into a photos-only job"


def test_the_kinds_left_out_are_counted_not_forgotten(console, mixed, tmp_path):
    client, _, _ = console
    result = estimate(client, mixed, ["image"], tmp_path / "Master")
    assert result["excluded"] == 4
    assert result["excluded_by_kind"] == {"video": 3, "audio": 1}


def test_a_global_media_types_cannot_widen_a_narrowed_source(console, mixed, tmp_path):
    """A folder asked to scan for stills stays that way whatever else is sent."""
    client, _, _ = console
    result = estimate(client, mixed, ["image"], tmp_path / "Master",
                      media_types=["image", "video", "audio"])
    assert result["files"] == 4


# -- the estimate says enough to catch a wrong selection --------------------

def test_the_estimate_breaks_the_total_down_by_kind(console, mixed, tmp_path):
    client, _, _ = console
    result = estimate(client, mixed, ["image", "video", "audio"],
                      tmp_path / "Master")
    assert result["files"] == 8
    assert result["by_kind"] == {"image": 4, "video": 3, "audio": 1}
    # Which is what makes a misconfigured job obvious: the byte total is
    # essentially all video, and now it says so.
    assert result["bytes_by_kind"]["video"] == 6 * MB
    assert result["bytes_by_kind"]["audio"] == MB


def test_the_estimate_reports_the_selection_it_used(console, mixed, tmp_path):
    client, _, _ = console
    result = estimate(client, mixed, ["image"], tmp_path / "Master")
    assert result["sources"] == [{"path": str(mixed), "types": ["image"]}]


def test_a_small_job_is_not_flagged_as_truncated(console, mixed, tmp_path):
    client, _, _ = console
    assert estimate(client, mixed, ["image"], tmp_path / "Master")["truncated"] is False


# -- a source that would archive nothing ------------------------------------

def test_a_source_with_no_kinds_selected_is_called_out(console, mixed, tmp_path):
    client, _, _ = console
    body = {"source_dirs": [{"path": str(mixed), "types": []}],
            "destination_dir": str(tmp_path / "Master")}
    notices = client.post("/api/archive/validate", json=body).get_json()["notices"]
    assert any("no kinds of file selected" in n for n in notices), notices


def test_a_normal_source_draws_no_such_notice(console, mixed, tmp_path):
    client, _, _ = console
    body = {"source_dirs": [{"path": str(mixed), "types": ["image"]}],
            "destination_dir": str(tmp_path / "Master")}
    notices = client.post("/api/archive/validate", json=body).get_json()["notices"]
    assert not any("no kinds of file selected" in n for n in notices), notices
