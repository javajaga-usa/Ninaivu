"""Regression tests for the storage findings of the 10 October 2026 audit.

What is here, in the order the audit listed it: a bulk turn of the files asks
for the password as deleting does (L1); the recycle bin never moves a file to
a place outside the library, links followed (L3); an import holds a zip member
to the size it declares and to the free space on the disk (L4); the image
model's cached weights are only ever a safetensors file (L5); a converted
stream cannot be started by a link on another site (L21); and a filename with
a line break in it downloads under a cleaned name rather than failing.
"""

from __future__ import annotations

import collections
import io
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from conftest import ADMIN, ids_of
from ninaivu.api import api as api_mod
from ninaivu.storage import recycle
from ninaivu.storage import importer as importer_mod


# ---------------------------------------------------------------------------
# L1: turning the files asks for the password, as deleting does
# ---------------------------------------------------------------------------

def test_turning_the_files_without_a_password_is_refused_like_deleting(as_admin):
    ids = ids_of(as_admin)[:2]
    turned = as_admin.post("/api/rotate", json={"ids": ids, "rotation": 90})
    deleted = as_admin.post("/api/delete", json={"ids": ids})
    assert turned.status_code == deleted.status_code == 401
    assert turned.get_json()["needs_password"] is True
    assert set(turned.get_json()) == set(deleted.get_json())
    assert "password" in turned.get_json()["error"]


def test_the_wrong_password_turns_nothing_and_is_written_down(as_admin, scanned):
    _, conn, _ = scanned
    asset_id = ids_of(as_admin)[0]
    row = conn.execute("SELECT root, rel_path FROM assets WHERE id=?", (asset_id,)).fetchone()
    path = Path(row["root"]) / row["rel_path"]
    before = path.read_bytes()
    answer = as_admin.post("/api/rotate", json={"ids": [asset_id], "rotation": 90,
                                                "password": "nope"})
    assert answer.status_code == 401
    assert "not right" in answer.get_json()["error"]
    assert path.read_bytes() == before
    assert "rotate_refused" in str(as_admin.get("/api/audit").get_json())


def test_with_the_password_the_files_are_turned(as_admin, scanned):
    _, conn, _ = scanned
    asset_id = ids_of(as_admin)[0]
    row = conn.execute("SELECT root, rel_path FROM assets WHERE id=?", (asset_id,)).fetchone()
    path = Path(row["root"]) / row["rel_path"]
    before = path.read_bytes()
    answer = as_admin.post("/api/rotate", json={"ids": [asset_id], "rotation": 90,
                                                "password": ADMIN[1]})
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["rotated"] == 1
    assert path.read_bytes() != before


# ---------------------------------------------------------------------------
# L3: the bin never moves a file outside the library
# ---------------------------------------------------------------------------

def _binned(as_admin, scanned):
    """One photograph in the bin; returns (conn, root, entry, file in the bin)."""
    _, conn, _ = scanned
    target = ids_of(as_admin)[0]
    row = conn.execute("SELECT root, rel_path FROM assets WHERE id=?", (target,)).fetchone()
    as_admin.post("/api/visibility", json={"ids": [target], "visibility": "hidden"})
    answer = as_admin.post("/api/delete", json={"ids": [target], "password": ADMIN[1]})
    assert answer.get_json()["deleted"] == 1, answer.get_json()
    entry = recycle.listing(conn)[0]
    assert Path(entry["bin_path"]).exists()
    return conn, Path(row["root"]), entry, Path(entry["bin_path"])


def test_an_entry_whose_path_climbs_out_of_the_library_is_not_restored(as_admin, scanned, tmp_path):
    conn, root, entry, binned = _binned(as_admin, scanned)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    climb = os.path.relpath(outside / "photo.jpg", root).replace(os.sep, "/")
    assert climb.startswith("..")
    conn.execute("UPDATE recycled SET rel_path=? WHERE id=?", (climb, entry["id"]))
    conn.commit()

    result = recycle.restore(conn, [entry["id"]])

    assert result["restored"] == 0
    assert result["failed"] and "not inside the library" in result["failed"][0]["why"]
    assert binned.exists(), "the file left the bin"
    assert not (outside / "photo.jpg").exists()
    assert list(outside.iterdir()) == [], "folders were made outside the library"


def test_a_folder_that_became_a_link_elsewhere_is_not_restored_into(as_admin, scanned, tmp_path):
    conn, root, entry, binned = _binned(as_admin, scanned)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    folder = root / Path(entry["rel_path"]).parent
    if folder == root:
        pytest.skip("the photograph was at the top of the library")
    shutil.rmtree(folder)
    try:
        os.symlink(outside, folder, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this account cannot make a folder link: {exc}")

    result = recycle.restore(conn, [entry["id"]])

    assert result["restored"] == 0
    assert "not inside the library" in result["failed"][0]["why"]
    assert binned.exists()
    assert list(outside.iterdir()) == []


def test_an_entry_pointing_at_a_file_still_in_the_library_cannot_move_it(as_admin, scanned):
    conn, root, entry, _ = _binned(as_admin, scanned)
    other = conn.execute("SELECT root, rel_path FROM assets LIMIT 1").fetchone()
    victim = Path(other["root"]) / other["rel_path"]
    assert victim.exists()
    conn.execute("UPDATE recycled SET bin_path=? WHERE id=?", (str(victim), entry["id"]))
    conn.commit()

    result = recycle.restore(conn, [entry["id"]])

    assert result["restored"] == 0
    assert "not inside the recycle bin" in result["failed"][0]["why"]
    assert victim.exists(), "a photograph in the library was moved"


def test_deleting_through_a_link_that_leaves_the_library_is_refused(scanned, tmp_path):
    _, conn, _ = scanned
    root = Path(scanned[0].active_root)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    Image.new("RGB", (8, 8), "red").save(outside / "away.jpg")
    try:
        os.symlink(outside, root / "linked", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this account cannot make a folder link: {exc}")
    asset_id = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, visibility) "
        "VALUES(?,?,?,?,?,?,?,1)",
        (str(root), "linked/away.jpg", "away.jpg", "linked", "jpg", "picture", 100)).lastrowid
    conn.commit()

    result = recycle.recycle(conn, [asset_id])

    assert result["deleted"] == 0
    assert "not inside the library" in result["failed"][0]["why"]
    assert (outside / "away.jpg").exists(), "a file outside the library was moved"


# ---------------------------------------------------------------------------
# L4: an import holds a member to the size it declares and the disk's room
# ---------------------------------------------------------------------------

def _jpeg(colour) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (120, 80), colour).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture()
def export(scanned, tmp_path):
    cfg, conn, _ = scanned
    cfg.watch = False
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    with zipfile.ZipFile(downloads / "takeout-20261010-001.zip", "w") as z:
        z.writestr("Takeout/Google Photos/Photos from 2019/IMG_0101.jpg", _jpeg((10, 120, 200)))
        z.writestr("Takeout/Google Photos/Photos from 2019/IMG_0102.jpg", _jpeg((200, 30, 30)))
    from ninaivu.storage import db
    importer = importer_mod.Importer(cfg, lambda: db.connect(cfg.db_path))
    return {"cfg": cfg, "conn": conn, "downloads": downloads, "importer": importer}


def _run(export):
    importer = export["importer"]
    assert importer.start(str(export["downloads"]), export["cfg"].active_root, None) == {"started": True}
    importer.join(60)
    return importer.status()


def _landed(export) -> list[Path]:
    library = Path(export["cfg"].active_root)
    return sorted(p for p in library.rglob("IMG_010*.jpg"))


def test_a_member_that_streams_past_its_declared_size_is_skipped_and_named(export, monkeypatch):
    real = importer_mod.ZipSource.members

    def lying(self):
        # The zip's table of contents says one member is a few bytes; the
        # stream behind it is the whole photograph, as a zip bomb's would be.
        for member in real(self):
            if member.name.endswith("IMG_0101.jpg"):
                member.size = 16
            yield member
    monkeypatch.setattr(importer_mod.ZipSource, "members", lying)

    status = _run(export)

    assert status["failed"] == 1 and status["copied"] == 1, status
    assert status["problems"][0]["file"].endswith("IMG_0101.jpg")
    assert "16 bytes" in status["problems"][0]["why"]
    assert [p.name for p in _landed(export)] == ["IMG_0102.jpg"]
    assert not list(Path(export["cfg"].active_root).rglob("*.part")), "a part was left behind"


def test_a_member_that_declares_more_than_ninaivu_takes_is_skipped(export, monkeypatch):
    from ninaivu.media import phone_backup
    real = importer_mod.ZipSource.members

    def enormous(self):
        for member in real(self):
            if member.name.endswith("IMG_0102.jpg"):
                member.size = phone_backup.MAX_FILE_BYTES + 1
            yield member
    monkeypatch.setattr(importer_mod.ZipSource, "members", enormous)

    status = _run(export)

    assert status["failed"] == 1 and status["copied"] == 1, status
    assert status["problems"][0]["file"].endswith("IMG_0102.jpg")
    assert "larger than Ninaivu imports" in status["problems"][0]["why"]


def test_when_the_disk_is_nearly_full_nothing_is_written(export, monkeypatch):
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10 ** 12, 10 ** 12, 1024))

    status = _run(export)

    assert status["copied"] == 0 and status["failed"] == 2, status
    assert all("free space" in problem["why"] for problem in status["problems"])
    assert _landed(export) == []
    reasons = [r[0] for r in export["conn"].execute("SELECT reason FROM imports WHERE state='failed'")]
    assert len(reasons) == 2 and all("free space" in r for r in reasons)


# ---------------------------------------------------------------------------
# L5: only a safetensors file is ever handed to open_clip
# ---------------------------------------------------------------------------

REPO = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
TAG = ("ViT-B-32", "laion2b_s34b_b79k")


@pytest.fixture()
def weights_cache(monkeypatch, tmp_path):
    """A stand-in for the Hugging Face cache: repo/filename -> a file, if there.

    Through the real open_clip and huggingface_hub where they import, and
    through the two functions this code reads from them where they do not
    (the core install leaves search by description out, and a torch whose
    DLLs do not load on a machine is as absent as none): the rule under test
    is Ninaivu's, not theirs.
    """
    import sys
    import types
    present: dict[tuple[str, str], str] = {}

    def lookup(repo, filename, *args, **kwargs):
        return present.get((repo, filename))
    try:
        import huggingface_hub
        import open_clip  # noqa: F401
        from open_clip import constants  # noqa: F401
    except ImportError:
        hub = types.ModuleType("huggingface_hub")
        clip = types.ModuleType("open_clip")
        constants = types.ModuleType("open_clip.constants")
        constants.HF_WEIGHTS_NAME = "open_clip_pytorch_model.bin"
        constants.HF_SAFE_WEIGHTS_NAME = "open_clip_model.safetensors"
        clip.constants = constants
        clip.get_pretrained_cfg = lambda model, tag: (
            {"hf_hub": REPO + "/", "mean": (0.48145466, 0.4578275, 0.40821073),
             "std": (0.26862954, 0.26130258, 0.27577711),
             "interpolation": "bicubic", "resize_mode": "shortest"}
            if (model, tag) == TAG else {})
        monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
        monkeypatch.setitem(sys.modules, "open_clip", clip)
        monkeypatch.setitem(sys.modules, "open_clip.constants", constants)
        huggingface_hub = hub
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lookup, raising=False)

    def put(filename):
        path = tmp_path / filename
        path.write_bytes(b"weights")
        present[(REPO, filename)] = str(path)
        return str(path)
    return put


def test_a_cache_holding_only_a_pickle_hands_nothing_over(weights_cache):
    from ninaivu import ai
    for name in ("open_clip_pytorch_model.bin", "pytorch_model.bin", "open_clip_pytorch_model.pth"):
        weights_cache(name)
    assert ai.cached_weights(*TAG) is None


def test_the_safetensors_file_is_handed_over_with_the_tags_preparation(weights_cache):
    from ninaivu import ai
    weights_cache("open_clip_pytorch_model.bin")
    safe = weights_cache("open_clip_model.safetensors")
    found, preparation = ai.cached_weights(*TAG)
    assert found == safe
    assert preparation["image_interpolation"] == "bicubic"


# ---------------------------------------------------------------------------
# L21: a converted stream cannot be started from another site
# ---------------------------------------------------------------------------

class _IdleStore:
    """A proxy store with nothing made and a record of what it was asked to start."""

    def __init__(self):
        self.started = []

    def ready(self, asset_id, source):
        return None

    def start(self, asset_id, source, duration=None, kind=None):
        self.started.append(asset_id)
        return type("State", (), {"state": "working", "payload": lambda self: {"progress": 0}})()


@pytest.fixture()
def clip(as_family, scanned, monkeypatch):
    cfg, conn, _ = scanned
    library = Path(cfg.active_root)
    (library / "misc").mkdir(exist_ok=True)
    (library / "misc" / "camcorder.mkv").write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * 64)
    asset_id = conn.execute(
        "INSERT INTO assets(root, rel_path, filename, folder, ext, kind, size, duration, visibility) "
        "VALUES(?,?,?,?,?,?,?,?,1)",
        (str(library), "misc/camcorder.mkv", "camcorder.mkv", "misc", "mkv", "video", 68, 2.0)).lastrowid
    conn.commit()
    store = _IdleStore()
    monkeypatch.setattr(api_mod, "_proxy_store", lambda: store)
    played = []

    def live(source, kind, **kwargs):
        played.append(source)
        yield b"\x00\x00\x00\x18ftyp"
    monkeypatch.setattr(api_mod.proxies, "live", live)
    return {"client": as_family, "id": asset_id, "store": store, "played": played}


def test_a_link_on_another_site_cannot_start_an_encode(clip):
    for url in (f"/api/stream/{clip['id']}", f"/api/proxy/{clip['id']}"):
        answer = clip["client"].get(url, headers={"Sec-Fetch-Site": "cross-site"})
        assert answer.status_code == 403, url
        assert answer.get_json() == {"error": "Cross-origin request refused.", "status": 403}
    assert clip["store"].started == [] and clip["played"] == []


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "same-origin"},
                                     {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "none"}])
def test_the_viewers_own_player_and_a_direct_player_still_get_the_stream(clip, headers):
    answer = clip["client"].get(f"/api/stream/{clip['id']}", headers=headers)
    assert answer.status_code == 200 and answer.mimetype == "video/mp4"
    assert answer.data[4:8] == b"ftyp"
    assert clip["store"].started == [clip["id"]]
    proxy = clip["client"].get(f"/api/proxy/{clip['id']}", headers=headers)
    assert proxy.status_code == 202


# ---------------------------------------------------------------------------
# A filename with a line break downloads under a cleaned name, not a 500
# ---------------------------------------------------------------------------

def test_a_filename_with_a_line_break_in_it_still_downloads(as_admin, scanned):
    _, conn, _ = scanned
    asset_id = ids_of(as_admin)[0]
    conn.execute("UPDATE assets SET filename=? WHERE id=?",
                 ("holiday\r\nX-Injected: yes.jpg", asset_id))
    conn.commit()

    answer = as_admin.get(f"/api/download/{asset_id}")

    assert answer.status_code == 200
    disposition = answer.headers["Content-Disposition"]
    assert "attachment" in disposition and "holiday" in disposition
    assert "\r" not in disposition and "\n" not in disposition
    assert "X-Injected" not in answer.headers
    answer.close()


def test_a_file_served_as_a_download_with_such_a_name_is_not_a_500(as_admin, scanned):
    """``/api/file`` sends a type it will not render as a download, named too."""
    _, conn, _ = scanned
    asset_id = ids_of(as_admin)[0]
    row = conn.execute("SELECT root, rel_path FROM assets WHERE id=?", (asset_id,)).fetchone()
    blob = Path(row["root"]) / "misc" / "blob.bin"
    blob.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(row["root"]) / row["rel_path"], blob)
    conn.execute("UPDATE assets SET filename=?, ext='bin', rel_path='misc/blob.bin' WHERE id=?",
                 ("holiday\r\nX-Injected: yes.bin", asset_id))
    conn.commit()

    answer = as_admin.get(f"/api/file/{asset_id}")

    assert answer.status_code == 200, answer.status_code
    disposition = answer.headers["Content-Disposition"]
    assert "attachment" in disposition and "\n" not in disposition
    answer.close()


def test_the_cleaning_keeps_the_name_and_takes_only_the_control_characters():
    assert api_mod.download_name("a\r\nb\tc.jpg") == "a  b c.jpg"
    assert api_mod.download_name("நாள்.jpg") == "நாள்.jpg"
