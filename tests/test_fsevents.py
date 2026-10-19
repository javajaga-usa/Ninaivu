"""What changed while Ninaivu was not running, from the macOS FSEvents journal.

ninaivu/storage/fsevents.py. The sorting of events into folders (or into a
reason to walk everything) is plain Python and is tested everywhere; asking
the real journal is tested on a Mac only.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from ninaivu.storage import fsevents
from ninaivu.storage.fsevents import (
    HISTORY_DONE, KERNEL_DROPPED, MOUNT, MUST_SCAN_SUBDIRS, USER_DROPPED, History, Unsupported,
    _Collector,
)

on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="FSEvents is macOS only")
not_mac = pytest.mark.skipif(sys.platform == "darwin", reason="checks the non-Mac answers")


# --- anywhere -----------------------------------------------------------------

def _collector(root, **kw):
    return _Collector([str(root)], 100, kw.get("max_folders", 5000))


def test_folders_under_the_library_are_kept_and_others_ignored(tmp_path):
    root = os.path.realpath(tmp_path)
    c = _collector(root)
    c.add(root + "/2024/05/", 0, 101)
    c.add(root + "/2024/06", 0, 105)
    c.add("/somewhere/else", 0, 110)
    c.add(root + "-sibling/x", 0, 111)          # a prefix, not a parent
    c.add("", HISTORY_DONE, 0)
    h = c.history()
    assert h == History(frozenset({root + "/2024/05", root + "/2024/06"}), True, 111, "")
    assert c.finished.is_set()


def test_a_symlinked_library_matches_either_spelling(tmp_path):
    real = tmp_path / "real"
    (real / "a").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    c = _Collector([str(link)], 1, 5000)
    c.add(os.path.realpath(real) + "/a", 0, 2)
    c.add(str(link) + "/a", 0, 3)
    c.add("", HISTORY_DONE, 0)
    assert c.history().folders == {os.path.realpath(real) + "/a", str(link) + "/a"}


@pytest.mark.parametrize("flag", [USER_DROPPED, KERNEL_DROPPED, MOUNT])
def test_lost_or_unsure_events_mean_walk_everything(tmp_path, flag):
    c = _collector(tmp_path)
    c.add(str(tmp_path) + "/a", flag, 101)
    h = c.history()
    assert not h.complete and h.reason
    assert c.finished.is_set()


def test_rescan_at_or_above_the_library_means_walk_everything(tmp_path):
    c = _collector(tmp_path)
    c.add("/", MUST_SCAN_SUBDIRS, 101)
    assert not c.history().complete
    # Below the library it is just a folder to read with what is under it.
    c = _collector(tmp_path)
    c.add(str(tmp_path) + "/deep", MUST_SCAN_SUBDIRS, 101)
    c.add("", HISTORY_DONE, 0)
    h = c.history()
    assert h.complete and h.folders == {str(tmp_path) + "/deep"}


def test_too_many_folders_is_not_worth_it(tmp_path):
    c = _collector(tmp_path, max_folders=3)
    for i in range(5):
        c.add(f"{tmp_path}/{i}", 0, 101 + i)
    h = c.history()
    assert not h.complete and h.reason == "too many changes"


def test_no_end_of_history_is_never_complete(tmp_path):
    c = _collector(tmp_path)
    c.add(str(tmp_path) + "/a", 0, 101)
    assert c.history(timed_out=True).reason == "timed out"
    assert not c.history().complete


# --- not a Mac ----------------------------------------------------------------

@not_mac
def test_elsewhere_there_is_no_journal(tmp_path):
    assert fsevents.available() is False
    assert fsevents.volume_id(tmp_path) is None
    with pytest.raises(Unsupported):
        fsevents.current_event_id()
    with pytest.raises(Unsupported):
        fsevents.changes_since([str(tmp_path)], 123)


# --- a Mac --------------------------------------------------------------------

@on_mac
def test_the_volume_has_a_journal(tmp_path):
    assert fsevents.available()
    vid = fsevents.volume_id(tmp_path)
    if vid is None:
        pytest.skip("this volume keeps no FSEvents history")
    assert isinstance(vid, str) and len(vid) >= 32


@on_mac
def test_no_stored_id_means_walk_everything(tmp_path):
    h = fsevents.changes_since([os.path.realpath(tmp_path)], 0)
    assert not h.complete


@on_mac
def test_a_new_folder_is_reported(tmp_path):
    root = os.path.realpath(tmp_path)
    if fsevents.volume_id(root) is None:
        pytest.skip("this volume keeps no FSEvents history")
    (tmp_path / "before.jpg").write_bytes(b"x")
    since = fsevents.current_event_id()
    sub = os.path.join(root, "2024", "05")
    os.makedirs(sub)
    with open(os.path.join(sub, "new.jpg"), "wb") as f:
        f.write(b"photo")
    with open(os.path.join(root, "before.jpg"), "ab") as f:
        f.write(b"more")
    os.sync()
    deadline = time.monotonic() + 10
    while True:
        h = fsevents.changes_since([root], since, timeout=30)
        assert h.complete, h.reason
        if sub in h.folders or time.monotonic() > deadline:
            break
        time.sleep(0.25)
    assert sub in h.folders
    assert h.last_id > since
