"""Fixes for the external audit of 25 November 2026 (commit 791f50c).

F1: a restore, import or second-copy restore on a drive without hard links
checked the name was free and then renamed onto it. A file made in between
was replaced. Each case here makes the name exactly in that window.
"""

import errno
import os

import pytest

from ninaivu.storage import db
from ninaivu.cloud import restore
from ninaivu.storage import importer as importer_mod
from ninaivu.utils import files as files_mod

from test_cloud_restore import lose_the_library, run, upload, world  # noqa: F401


def _no_links(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError(errno.EPERM, "Operation not permitted (exFAT)")
    monkeypatch.setattr(files_mod.os, "link", refuse)


def _taken_in_the_window(monkeypatch, make, *, atomic_rename=True):
    """Make the destination just before the no-hard-link step runs. With
    *atomic_rename* False the system also has no rename that refuses a
    name (exFAT on macOS), so the exclusive copy is what is tested."""
    real = files_mod._rename_no_replace
    done = []

    def racing(src, dst):
        if not done:
            done.append(dst)
            make(dst)
        return real(src, dst) if atomic_rename else False
    monkeypatch.setattr(files_mod, "_rename_no_replace", racing)
    return done


def _new_original(path):
    with open(path, "wb") as handle:
        handle.write(b"NEW ORIGINAL")


# -- the primitive ----------------------------------------------------------

@pytest.mark.parametrize("atomic_rename", [True, False])
@pytest.mark.parametrize("kind", ["file", "folder", "link"])
def test_a_name_taken_in_the_window_is_never_replaced(tmp_path, monkeypatch,
                                                       atomic_rename, kind):
    if kind == "link" and os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    staged = tmp_path / "photo.part"
    staged.write_bytes(b"RESTORED")
    target = tmp_path / "photo.jpg"
    elsewhere = tmp_path / "elsewhere.jpg"

    def make(dst):
        if kind == "file":
            _new_original(dst)
        elif kind == "folder":
            os.mkdir(dst)
        else:
            os.symlink(elsewhere, dst)          # dangling: never written through

    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, make, atomic_rename=atomic_rename)
    with pytest.raises(FileExistsError):
        files_mod.publish_no_replace(staged, target)

    assert staged.read_bytes() == b"RESTORED", "the staged copy was lost"
    if kind == "file":
        assert target.read_bytes() == b"NEW ORIGINAL"
    elif kind == "folder":
        assert target.is_dir() and not any(target.iterdir())
    else:
        assert target.is_symlink() and not elsewhere.exists()


@pytest.mark.parametrize("atomic_rename", [True, False])
def test_without_hard_links_a_free_name_still_gets_the_file(tmp_path, monkeypatch,
                                                            atomic_rename):
    staged = tmp_path / "photo.part"
    staged.write_bytes(b"RESTORED")
    os.utime(staged, (1_262_304_000, 1_262_304_000))
    target = tmp_path / "photo.jpg"
    _no_links(monkeypatch)
    if not atomic_rename:
        monkeypatch.setattr(files_mod, "_rename_no_replace", lambda *a: False)
    files_mod.publish_no_replace(staged, target)
    assert target.read_bytes() == b"RESTORED" and not staged.exists()
    assert abs(target.stat().st_mtime - 1_262_304_000) < 2


def test_a_copy_that_fails_part_way_leaves_nothing_and_keeps_the_staged_file(
        tmp_path, monkeypatch):
    staged = tmp_path / "photo.part"
    staged.write_bytes(b"RESTORED" * 1000)
    target = tmp_path / "photo.jpg"
    _no_links(monkeypatch)
    monkeypatch.setattr(files_mod, "_rename_no_replace", lambda *a: False)
    monkeypatch.setattr(files_mod.os, "fsync",
                        lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")))
    with pytest.raises(OSError):
        files_mod.publish_no_replace(staged, target)
    assert not target.exists() and staged.exists()


# -- the restore, the import and the second copy -------------------------------

@pytest.mark.parametrize("atomic_rename", [True, False])
def test_a_restore_never_replaces_a_photograph_imported_meanwhile(world, monkeypatch,  # noqa: F811
                                                                  atomic_rename):
    db_path, _, client, library, files, _ = world
    upload(world)
    lose_the_library(library)
    items = [i for i in restore.from_record(db.connect(db_path))
             if i.rel_path == "2021/garden.jpg"]
    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, _new_original, atomic_rename=atomic_rename)

    state = run(restore.RestoreJob(connect=lambda: client, items=items,
                                   destination=None))

    assert (library / "2021/garden.jpg").read_bytes() == b"NEW ORIGINAL"
    assert (library / "2021/garden (restored).jpg").read_bytes() == files["2021/garden.jpg"]
    assert state["failed"] == 0 and state["beside"] == 1


@pytest.mark.parametrize("atomic_rename", [True, False])
def test_an_import_takes_the_next_name_when_one_appears_meanwhile(tmp_path, monkeypatch,
                                                                  atomic_rename):
    partial = tmp_path / "x.part"
    partial.write_bytes(b"IMPORTED")
    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, _new_original, atomic_rename=atomic_rename)
    target = importer_mod._publish(partial, tmp_path / "a.jpg")
    assert (tmp_path / "a.jpg").read_bytes() == b"NEW ORIGINAL"
    assert target.name == "a (2).jpg" and target.read_bytes() == b"IMPORTED"


def test_a_date_change_without_hard_links_never_replaces_a_file_made_meanwhile(
        tmp_path, monkeypatch):
    from ninaivu.media import date_edit
    source = tmp_path / "a.jpg"
    source.write_bytes(b"MOVED")
    target = tmp_path / "b.jpg"
    _no_links(monkeypatch)
    _taken_in_the_window(monkeypatch, _new_original)
    with pytest.raises(FileExistsError):
        date_edit._move(source, target)
    assert target.read_bytes() == b"NEW ORIGINAL" and source.read_bytes() == b"MOVED"
