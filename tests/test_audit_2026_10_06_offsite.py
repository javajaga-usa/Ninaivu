"""The off-site copy, encrypted backups and their restores: the 2026-10-06 audit.

A-05 a changed or missing destination, A-06 versions instead of overwrites,
A-07 the manifest is merged and never replaced under another key, A-13 staged
files are never written through a link, A-14 the key settings beside the
off-site copy and one per key in Drive, A-15 the stand-alone tools need only
``cryptography``, A-16 a restore from the index copy finds what went up
since, A-34 the SHA-256 is checked, A-35 an upload is confirmed, A-36 the
key file and the upload cache.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninaivu.cloud import crypto, keyring, offsite as offsite_mod, restore, s3
from ninaivu.cloud.offsite import MANIFEST, MANIFEST_PREV, MARKER, Offsite, read_manifest
from ninaivu.storage import db

pytest.importorskip("cryptography")

REPO = Path(__file__).resolve().parents[1]
PASSPHRASE = "correct horse battery"


@pytest.fixture()
def lib(scanned, tmp_path):
    cfg, conn, _ = scanned
    keyring.create(cfg.state_dir, PASSPHRASE, PASSPHRASE)
    cfg.offsite_kind = "folder"
    cfg.offsite_folder = str(tmp_path / "disk-1")
    Path(cfg.offsite_folder).mkdir()
    off = Offsite(cfg, lambda: db.connect(cfg.db_path))
    return SimpleNamespace(cfg=cfg, conn=conn, off=off, tmp=tmp_path)


def run(off, work=None):
    (work or off.start)()
    off.join(60)
    return off.status()


def base(cfg) -> Path:
    return Path(cfg.offsite_folder) / "ninaivu-offsite"


def objects(cfg) -> dict[str, bytes]:
    return {p.relative_to(base(cfg)).as_posix(): p.read_bytes()
            for p in (base(cfg) / "files").rglob("*.ninaivu")}


def library_files(cfg) -> dict[str, bytes]:
    root = Path(cfg.active_root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not p.name.endswith(".txt")}


def tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in folder.rglob("*") if p.is_file()}


def manifest(lib) -> list[dict]:
    key, _ = keyring.key_material(keyring.load(lib.cfg.state_dir))
    return read_manifest(offsite_mod.FolderTarget(lib.cfg.offsite_folder), key,
                         lib.tmp / "staging")


def recovery_file(lib, name="recovery.json") -> Path:
    path = lib.tmp / name
    path.write_text(json.dumps(keyring.recovery_document(keyring.load(lib.cfg.state_dir))))
    return path


def a_photo(lib) -> Path:
    return next(Path(lib.cfg.active_root).rglob("shot1.jpg"))


def change(lib, path: Path, data: bytes) -> None:
    """Change a file in the library, and tell the index, as a rescan would."""
    path.write_bytes(data)
    stat = path.stat()
    rel = path.relative_to(lib.cfg.active_root).as_posix()
    lib.conn.execute("UPDATE assets SET size=?, mtime=? WHERE rel_path=?",
                     (stat.st_size, stat.st_mtime, rel))
    lib.conn.commit()


# -- A-05: the destination is known by a marker -------------------------------------

def test_a_new_folder_gets_everything_not_a_report_that_it_is_kept(lib):
    first = run(lib.off)
    assert first["kept"] == first["files"] > 0 and not first["error"]
    assert (base(lib.cfg) / MARKER).is_file()

    lib.cfg.offsite_folder = str(lib.tmp / "disk-2")
    Path(lib.cfg.offsite_folder).mkdir()
    assert lib.off.status()["kept"] == 0, "nothing has been sent to the new folder yet"
    second = run(lib.off)
    assert second["sent"] == first["files"] and second["kept"] == first["files"], second
    out = lib.tmp / "out"
    assert run(lib.off, lambda: lib.off.restore(str(out)))["sent"] == first["files"]
    assert tree(out) == library_files(lib.cfg)


def test_an_unmounted_disk_is_refused_not_filled_on_the_system_disk(lib):
    sent = run(lib.off)["files"]
    shutil.rmtree(base(lib.cfg))            # the mount point, left empty by an unplugged disk
    problem = lib.off.problem()
    assert problem and "mounted" in problem and f"{sent:,}" in problem
    with pytest.raises(ValueError, match="mounted"):
        lib.off.start()
    assert not base(lib.cfg).exists(), "nothing was written to the empty mount point"
    assert not lib.off.due()

    # Gone on purpose: starting over is the administrator's to choose.
    again = run(lib.off, lambda: lib.off.start(start_over=True))
    assert again["sent"] == sent and not again["error"]


def test_a_different_disk_at_the_same_path_is_sent_everything(lib):
    sent = run(lib.off)["files"]
    shutil.rmtree(base(lib.cfg))
    base(lib.cfg).mkdir()
    (base(lib.cfg) / MARKER).write_text(json.dumps({"id": "another-disk"}))
    status = run(lib.off)
    assert status["sent"] == sent and status["kept"] == sent and not status["error"]
    assert json.loads((base(lib.cfg) / MARKER).read_text())["id"] == "another-disk"


def test_two_disks_taken_in_turn_are_each_sent_only_what_they_lack(lib):
    disk_1 = lib.cfg.offsite_folder
    sent = run(lib.off)["files"]
    lib.cfg.offsite_folder = str(lib.tmp / "disk-2")
    Path(lib.cfg.offsite_folder).mkdir()
    assert run(lib.off)["sent"] == sent
    lib.cfg.offsite_folder = disk_1
    back = run(lib.off)
    assert back["sent"] == 0 and back["kept"] == sent and not back["error"], back


def test_a_missing_object_is_sent_again(lib):
    run(lib.off)
    lib.off.SPOT_CHECK = 1000
    gone = next(iter(objects(lib.cfg)))
    (base(lib.cfg) / gone).unlink()
    status = run(lib.off)
    assert status["sent"] == 1 and (base(lib.cfg) / gone).is_file()


def test_a_copy_from_before_markers_is_kept_not_sent_again(lib):
    sent = run(lib.off)["files"]
    (base(lib.cfg) / MARKER).unlink()
    conn = db.connect(lib.cfg.db_path)
    conn.execute("DELETE FROM offsite_meta WHERE key IN ('dest_id', 'dest_where', 'key_id')")
    conn.commit()
    status = run(lib.off)
    assert status["sent"] == 0 and status["kept"] == sent and (base(lib.cfg) / MARKER).is_file()


# -- A-06: a changed file never replaces the copy of the old one ---------------------

def test_a_damaged_file_does_not_replace_the_good_copy(lib):
    run(lib.off)
    photo = a_photo(lib)
    good = photo.read_bytes()
    before = objects(lib.cfg)
    change(lib, photo, good[: len(good) // 2])              # truncated here
    assert run(lib.off)["sent"] == 1
    after = objects(lib.cfg)
    assert all(after[name] == data for name, data in before.items()), "an object was overwritten"
    assert len(after) == len(before) + 1
    rel = photo.relative_to(lib.cfg.active_root).as_posix()
    assert len([e for e in manifest(lib) if e["path"] == rel]) == 2

    # The newest by default; every version when asked, the older beside it.
    from ninaivu.cli import offsite_restore as tool
    newest, every = lib.tmp / "newest", lib.tmp / "every"
    assert tool.main(["--recovery", str(recovery_file(lib)), str(lib.cfg.offsite_folder),
                      str(newest)]) == 0
    assert (newest / rel).read_bytes() == good[: len(good) // 2]
    assert tool.main(["--recovery", str(recovery_file(lib)), "--all-versions",
                      str(lib.cfg.offsite_folder), str(every)]) == 0
    beside = (every / rel).with_name(f"{photo.stem} (restored){photo.suffix}")
    assert (every / rel).read_bytes() == good[: len(good) // 2]
    assert beside.read_bytes() == good


def test_only_a_new_time_sends_nothing(lib):
    run(lib.off)
    photo = a_photo(lib)
    later = time.time() + 100
    os.utime(photo, (later, later))
    change(lib, photo, photo.read_bytes())
    os.utime(photo, (later, later))
    lib.conn.execute("UPDATE assets SET mtime=? WHERE filename='shot1.jpg'", (later,))
    lib.conn.commit()
    status = run(lib.off)
    assert status["sent"] == 0 and status["kept"] == status["files"], status


def test_a_copy_in_the_old_format_still_comes_back(lib, tmp_path):
    """One object a path, named without the contents, and a manifest without versions."""
    key, key_id = keyring.key_material(keyring.load(lib.cfg.state_dir))
    copy = tmp_path / "old-copy"
    photo = a_photo(lib)
    root = str(lib.cfg.active_root)
    rel = photo.relative_to(root).as_posix()
    name = offsite_mod.object_name(key, root, rel)
    (copy / name).parent.mkdir(parents=True)
    crypto.encrypt_file_v2(photo, copy / name, key, key_id)
    plain = tmp_path / "m.json"
    plain.write_text(json.dumps({"format": "ninaivu-offsite", "version": 1, "key_id": key_id.hex(),
                                 "files": [{"o": name, "library": Path(root).name, "root": root,
                                            "path": rel, "size": photo.stat().st_size,
                                            "mtime": photo.stat().st_mtime}]}))
    crypto.encrypt_file_v2(plain, copy / MANIFEST, key, key_id)
    from ninaivu.cli import offsite_restore as tool
    out = tmp_path / "out"
    assert tool.main(["--recovery", str(recovery_file(lib)), str(copy), str(out)]) == 0
    assert (out / rel).read_bytes() == photo.read_bytes()


# -- A-07: the manifest there is read first --------------------------------------------

def test_a_new_key_does_not_replace_the_manifest_made_with_the_old(lib):
    run(lib.off)
    old_recovery = recovery_file(lib, "old.json")
    sealed = (base(lib.cfg) / MANIFEST).read_bytes()
    keyring.path(lib.cfg.state_dir).unlink()
    keyring.create(lib.cfg.state_dir, "another long passphrase", "another long passphrase")
    status = run(lib.off)
    assert "another backup key" in status["error"] and "recovery file" in status["error"]
    assert (base(lib.cfg) / MANIFEST).read_bytes() == sealed, "the manifest was replaced"

    # The old key back from its recovery file, the new one kept beside it.
    with pytest.raises(ValueError, match="different backup key"):
        keyring.import_recovery(lib.cfg.state_dir, json.loads(old_recovery.read_text()))
    keyring.import_recovery(lib.cfg.state_dir, json.loads(old_recovery.read_text()),
                            replace=True)
    assert list(Path(lib.cfg.state_dir).glob("cloud-encryption.replaced-*.json"))
    status = run(lib.off)
    assert not status["error"] and status["kept"] == status["files"], status
    out = lib.tmp / "out"
    from ninaivu.cli import offsite_restore as tool
    assert tool.main(["--recovery", str(old_recovery), str(lib.cfg.offsite_folder), str(out)]) == 0


def test_a_fresh_index_does_not_shrink_the_manifest(lib):
    first = run(lib.off)
    conn = db.connect(lib.cfg.db_path)
    conn.execute("DELETE FROM offsite_copies")
    conn.execute("DELETE FROM offsite_meta")
    conn.execute("DELETE FROM assets WHERE filename='shot1.jpg'")
    conn.commit()
    status = run(lib.off)
    assert status["sent"] == 0 and not status["error"], "taken back from the manifest, not resent"
    paths = {e["path"] for e in manifest(lib)}
    assert any(p.endswith("shot1.jpg") for p in paths)
    assert len(paths) == first["files"]
    assert (base(lib.cfg) / MANIFEST_PREV).is_file()


def test_a_damaged_manifest_falls_back_to_the_one_before(lib):
    run(lib.off)
    run(lib.off)
    (base(lib.cfg) / MANIFEST).write_bytes(b"not a manifest")
    assert len(manifest(lib)) >= 1


def test_the_console_imports_a_recovery_file(app, as_admin, tmp_path):
    services = app.config["MV_SERVICES"]
    state = Path(services.cfg.state_dir)
    record = keyring.create(tmp_path, PASSPHRASE, PASSPHRASE)
    document = keyring.recovery_document(record)
    answer = as_admin.post("/api/cloud/encryption/import", json={"recovery": document})
    assert answer.status_code == 200 and answer.get_json()["changed"] is True
    assert keyring.load(state)["key_id"] == record["key_id"]
    assert as_admin.post("/api/cloud/encryption/import",
                         json={"recovery": document}).get_json()["changed"] is False
    other = keyring.recovery_document(keyring.create(tmp_path / "b", PASSPHRASE, PASSPHRASE))
    refused = as_admin.post("/api/cloud/encryption/import", json={"recovery": other})
    assert refused.status_code == 409 and refused.get_json()["needs_replace"] is True
    assert as_admin.post("/api/cloud/encryption/import",
                         json={"recovery": other, "replace": True}).status_code == 200
    assert keyring.load(state)["key_id"] == other["key_id"]
    assert as_admin.post("/api/cloud/encryption/import",
                         json={"recovery": {"key": "AAAA"}}).status_code == 400
    assert as_admin.post("/api/offsite/start", json={"start_over": "yes"}).status_code == 400


# -- A-13: nothing is written through a link at a staged name -------------------------

@pytest.mark.skipif(os.name == "nt", reason="links need privileges on Windows")
def test_a_link_at_the_staged_name_is_not_written_through(lib):
    run(lib.off)
    victim = lib.tmp / "victim.txt"
    victim.write_text("keep me")
    out = lib.tmp / "out"
    rel = a_photo(lib).relative_to(lib.cfg.active_root).as_posix()
    (out / rel).parent.mkdir(parents=True)
    (out / rel).parent.joinpath(f".{Path(rel).name}.ninaivu-part").symlink_to(victim)
    from ninaivu.cli import offsite_restore as tool
    assert tool.main(["--recovery", str(recovery_file(lib)), str(lib.cfg.offsite_folder),
                      str(out)]) == 0
    assert victim.read_text() == "keep me"
    assert (out / rel).read_bytes() == a_photo(lib).read_bytes()

    # Sending: a link at the .part name in the destination is not followed.
    target = offsite_mod.FolderTarget(lib.cfg.offsite_folder)
    (base(lib.cfg) / "probe.part").symlink_to(victim)
    target.put_bytes("probe", b"data")
    assert victim.read_text() == "keep me" and (base(lib.cfg) / "probe").read_bytes() == b"data"


@pytest.mark.skipif(os.name == "nt", reason="links need privileges on Windows")
def test_a_drive_restore_refuses_a_staged_download_that_is_a_link(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    part = tmp_path / "x.part"
    part.symlink_to(victim)
    job = restore.RestoreJob(connect=lambda: None, items=[], destination=tmp_path)
    item = restore.RestoreItem(remote_id="x", rel_path="a.jpg", size=4, stored_size=4,
                               sha256="0" * 64)
    with pytest.raises(ValueError, match="link"):
        job._download(SimpleNamespace(), item, part)
    assert victim.read_text() == "keep me" and not part.is_symlink()


# -- A-14: the passphrase is enough without Drive ---------------------------------------

def test_the_passphrase_is_enough_with_only_the_off_site_copy(lib):
    run(lib.off)
    record = keyring.load(lib.cfg.state_dir)
    params = json.loads((base(lib.cfg) / "ninaivu-encryption.json").read_text())
    assert params["key_id"] == record["key_id"] and "key" not in params
    assert (base(lib.cfg) / keyring.params_name(record)).is_file()
    from ninaivu.cli import offsite_restore as tool
    out = lib.tmp / "out"
    assert tool.main(["--passphrase", PASSPHRASE, str(lib.cfg.offsite_folder), str(out)]) == 0
    assert tree(out) == library_files(lib.cfg)


def test_a_later_key_gets_its_own_settings_file_in_drive(tmp_path):
    from fake_drive import FakeDrive
    from ninaivu.cloud import drive as drive_mod
    from ninaivu.cloud.service import CloudService

    fake = FakeDrive()
    client = drive_mod.DriveClient(creds=drive_mod.Credentials(
        client_id="c", client_secret="s", refresh_token="r", access_token="a",
        expires_at=9e9, folder_name="Ninaivu"), transport=fake)
    state = tmp_path / "state"
    state.mkdir()
    db.init_db(state / "index.db")
    cfg = SimpleNamespace(state_dir=state, cloud_folder_name="Ninaivu", cloud_encrypt=True,
                          cloud_rate_kbps=0, cloud_window_start="", cloud_window_end="")
    service = CloudService(cfg, lambda: db.connect(state / "index.db"))
    service.client = lambda: client
    first = keyring.create(state, PASSPHRASE, PASSPHRASE)
    service._publish_params(client)

    # A rebuilt machine: a new key, the same Drive folder.
    keyring.path(state).unlink()
    (state / CloudService.PARAMS_PLACED).unlink()
    second = keyring.create(state, "a second passphrase", "a second passphrase")
    service._publish_params(client)
    names = fake.uploaded_names()
    assert names.count(restore.PARAMS_NAME) == 1, "the first key's settings were kept"
    assert keyring.params_name(second) in names
    assert restore.find_params(client, second["key_id"])["key_id"] == second["key_id"]
    assert restore.find_params(client, first["key_id"])["key_id"] == first["key_id"]

    # The passphrase for either key finds its key, whichever is on this machine.
    keyring.path(state).unlink()
    assert keyring.key_id(service._key_from(None, PASSPHRASE)).hex() == first["key_id"]
    assert keyring.key_id(service._key_from(None, "a second passphrase")).hex() == second["key_id"]


# -- A-15: the stand-alone tools need nothing but cryptography ------------------------

BLOCKER = """
import sys, runpy
class Blocked:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("flask", "numpy", "cv2", "PIL", "pillow_heif", "werkzeug"):
            raise ImportError(f"{name} is not installed here")
        return None
sys.meta_path.insert(0, Blocked())
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
"""


def _bare(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    return subprocess.run([sys.executable, "-I", "-c", BLOCKER, *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)


def test_the_tools_run_with_only_cryptography(lib):
    run(lib.off)
    out = lib.tmp / "offsite-out"
    done = _bare(lib.tmp, str(REPO / "ninaivu" / "cli" / "offsite_restore.py"),
                 "--recovery", str(recovery_file(lib)), str(lib.cfg.offsite_folder), str(out))
    assert done.returncode == 0, done.stderr
    assert tree(out) == library_files(lib.cfg)

    key, key_id = keyring.key_material(keyring.load(lib.cfg.state_dir))
    downloaded = lib.tmp / "drive"
    downloaded.mkdir()
    crypto.encrypt_file_v2(a_photo(lib), downloaded / "shot1.jpg.ninaivu", key, key_id)
    params = lib.tmp / "params.json"
    params.write_text(json.dumps(keyring.public_params(keyring.load(lib.cfg.state_dir))))
    decrypted = lib.tmp / "decrypted"
    done = _bare(lib.tmp, str(REPO / "tools" / "cloud_decrypt.py"), "--params", str(params),
                 "--passphrase", PASSPHRASE, str(downloaded), str(decrypted))
    assert done.returncode == 0, done.stderr
    assert (decrypted / "shot1.jpg").read_bytes() == a_photo(lib).read_bytes()


# -- A-16: a restore from the index copy finds what went up since ------------------------

def test_files_sent_after_the_index_copy_are_restored_too(app, people, tmp_path):
    from test_index_copy import new_computer
    from ninaivu.cloud import store
    from ninaivu.cloud import drive as drive_mod
    from fake_drive import FakeDrive

    services = app.config["MV_SERVICES"]
    conn = db.connect(services.cfg.db_path)
    fake = FakeDrive()
    services.cloud.creds = drive_mod.Credentials(
        client_id="c", client_secret="s", refresh_token="r", access_token="a", expires_at=9e9)
    services.cloud.client = lambda: drive_mod.DriveClient(creds=services.cloud.creds,
                                                          transport=fake)
    engine = services.cloud.engine()
    engine.start()
    engine._thread.join(60)
    assert store.summary(conn)["done"] >= 10
    # The copy of the index is only ever sent encrypted; the photographs above
    # went before encryption was on, as a household's older uploads do.
    record = keyring.create(services.cfg.state_dir, "a long family passphrase",
                            "a long family passphrase")
    services.cfg.cloud_encrypt = True
    assert services.index_copy.run()["ok"]

    later = tmp_path / "later.jpg"
    later.write_bytes(b"a photograph sent after the copy of the index")
    client = services.cloud.client()
    folder = client.ensure_folder("2026", client.ninaivu_root())
    client.upload_file(later, later.name, folder)

    home = SimpleNamespace(services=services, cfg=services.cfg)
    service = new_computer(home, tmp_path)
    items = service.restore_items({"source": "drive"},
                                  recovery=keyring.recovery_document(record))
    assert service.index_found["newer_in_drive"] == 1
    newer = [i for i in items if i.rel_path == "2026/later.jpg"]
    assert len(newer) == 1 and newer[0].md5
    target = tmp_path / "rebuilt"
    service.restore_start(items, destination=str(target), key=None)
    service._restore.join(60)
    assert service.restore_status()["failed"] == 0
    assert (target / "2026" / "later.jpg").read_bytes() == later.read_bytes()


# -- A-34: the SHA-256 is checked, not only the size ----------------------------------

def test_an_older_version_put_in_place_of_the_newest_is_caught(lib):
    run(lib.off)
    photo = a_photo(lib)
    old = objects(lib.cfg)
    data = bytearray(photo.read_bytes())
    data[-3] ^= 0xFF                                    # the same size, other bytes
    change(lib, photo, bytes(data))
    run(lib.off)
    new = next(name for name in objects(lib.cfg) if name not in old)
    previous = next(e["o"] for e in manifest(lib)
                    if e["path"].endswith("shot1.jpg") and e["o"] != new)
    shutil.copyfile(base(lib.cfg) / previous, base(lib.cfg) / new)   # rolled back there
    from ninaivu.cli import offsite_restore as tool
    out = lib.tmp / "out"
    assert tool.main(["--recovery", str(recovery_file(lib)), str(lib.cfg.offsite_folder),
                      str(out)]) == 1
    rel = photo.relative_to(lib.cfg.active_root).as_posix()
    assert not (out / rel).exists()


# -- A-35: an upload is confirmed, not assumed ----------------------------------------

def test_an_upload_the_destination_does_not_have_is_a_problem(lib, monkeypatch):
    monkeypatch.setattr(offsite_mod.FolderTarget, "put_file",
                        lambda self, name, source, on_bytes=None, stop=None: None)
    status = run(lib.off)
    assert status["failed"] == status["files"] and status["kept"] == 0
    assert "not the" in status["problems"][0]["why"]


def test_a_multipart_completion_that_failed_in_its_body_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(s3, "PART", 4)
    answers = iter([b"<InitiateMultipartUploadResult><UploadId>u1</UploadId>"
                    b"</InitiateMultipartUploadResult>", b"", b"", b"",
                    b"<Error><Code>InternalError</Code><Message>try again</Message></Error>",
                    b""])
    sent = []

    def request(method, key="", *, query="", body=b"", headers=None):
        sent.append((method, query))
        return 200, {"ETag": '"e"'}, next(answers)

    client = s3.S3(endpoint="https://s3.example.com", bucket="b", access_key="k", secret_key="s")
    monkeypatch.setattr(client, "request", request)
    source = tmp_path / "big"
    source.write_bytes(b"0123456789")
    with open(source, "rb") as handle, pytest.raises(s3.S3Error, match="try again"):
        client._multipart("k", handle, None, None)
    assert sent[-1][0] == "DELETE", "the failed upload was not abandoned"


# -- A-36: the key file and the upload cache ------------------------------------------

def test_a_new_key_is_made_stronger_and_written_safely(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    if os.name != "nt":
        (state / "cloud-encryption.tmp").symlink_to(outside)
    record = keyring.create(state, PASSPHRASE, PASSPHRASE)
    assert record["n"] == 2 ** 17
    assert outside.read_text() == "keep me"
    assert not list(state.glob(".cloud-encryption-*.tmp"))
    # An older key's settings still make it again.
    old = {**keyring.public_params(record), "n": 2 ** 15}
    salt = __import__("base64").b64decode(old["salt"])
    old["key_id"] = keyring.key_id(keyring.derive(PASSPHRASE, salt, {"n": 2 ** 15})).hex()
    assert keyring.key_from(passphrase=PASSPHRASE, params=old)


def test_a_damaged_key_file_does_not_have_to_be_deleted_by_hand(tmp_path):
    keyring.path(tmp_path).write_text("{not json")
    assert keyring.load(tmp_path) is None
    keyring.create(tmp_path, PASSPHRASE, PASSPHRASE)
    assert keyring.load(tmp_path) is not None
    assert list(tmp_path.glob("cloud-encryption.damaged-*.json"))


def test_abandoned_ciphertext_is_cleared_from_the_upload_cache(tmp_path):
    from ninaivu.cloud.upload_cache import UploadCache

    cache = UploadCache(tmp_path / "cache")
    kept, orphan, fresh = (cache.path_for("/lib", name) for name in ("a", "b", "c"))
    half = cache.folder / ".ninaivu-crypto-x.tmp"
    stranger = cache.folder / "notes.txt"
    for path in (kept, orphan, fresh, half, stranger):
        path.write_bytes(b"x")
    old = time.time() - 7200
    for path in (kept, orphan, half, stranger):
        os.utime(path, (old, old))
    assert cache.prune({kept.name}) == 2
    assert kept.exists() and fresh.exists() and stranger.exists()
    assert not orphan.exists() and not half.exists()
