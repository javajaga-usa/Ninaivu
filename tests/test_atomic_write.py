"""One write-then-rename, synced, for files a power cut must not empty (audit C-24)."""

from __future__ import annotations

import os

import pytest

from ninaivu.utils import files


def test_it_replaces_whole(tmp_path):
    target = tmp_path / "applied.json"
    target.write_text("old", encoding="utf-8")
    files.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    files.atomic_write(target, b"\x00bytes")
    assert target.read_bytes() == b"\x00bytes"
    assert [p.name for p in tmp_path.iterdir()] == ["applied.json"]


def test_a_failed_write_leaves_the_old_file_and_no_part(tmp_path, monkeypatch):
    target = tmp_path / "applied.json"
    target.write_text("old", encoding="utf-8")

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        files.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["applied.json"]


def test_the_file_and_its_folder_are_synced(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    files.atomic_write(tmp_path / "x.json", "{}")
    assert len(synced) == (1 if os.name == "nt" else 2)
