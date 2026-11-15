"""Albums kept on a phone for when there is no signal (static/js/offline-albums.js,
static/sw.js "ALBUMS KEPT FOR OFFLINE")."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "ninaivu" / "static" / "js" / "offline-albums.js").read_text(encoding="utf-8")
WORKER = (ROOT / "ninaivu" / "static" / "sw.js").read_text(encoding="utf-8")


def test_the_worker_keeps_for_one_person_and_only_when_offline():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for service worker behavioral tests")
    subprocess.run([node, str(Path(__file__).with_name("offline_albums_sw.mjs"))],
                   check=True, timeout=30)


def test_hidden_and_explicit_photographs_are_never_kept_even_for_an_admin():
    assert "FLAG_EXPLICIT = 4" in PAGE and "FLAG_HIDDEN = 128" in PAGE
    assert "flags & (FLAG_EXPLICIT | FLAG_HIDDEN)" in PAGE


def test_a_guest_keeps_nothing():
    assert "['family', 'admin'].includes(user.role)" in PAGE
    assert "!user.anonymous" in PAGE


def test_only_thumbnails_are_kept_never_an_original():
    keep = WORKER[WORKER.index("async function keepAlbumNow"):WORKER.index("async function keptThumbnail")]
    assert "/api/thumb/" in keep
    for original in ("/api/file", "/api/download", "/api/proxy"):
        assert original not in keep


def test_the_kept_albums_survive_a_new_worker_but_not_a_sign_out():
    activate = WORKER[WORKER.index("'activate'"):WORKER.index("Signing out clears everything")]
    assert "name !== OFFLINE_CACHE" in activate
    forget = WORKER[WORKER.index("event.data === 'ninaivu:forget'"):]
    assert "startsWith('ninaivu-')" in forget[:400]


def test_it_is_on_unless_the_household_says_otherwise():
    from ninaivu.server.config import Config
    assert Config().offline_albums is True


def test_the_household_can_switch_it_off(app, as_family):
    app.config["MV_SERVICES"].cfg.offline_albums = True
    assert as_family.get("/api/status").get_json()["moments"]["offline_albums"] is True
    app.config["MV_SERVICES"].cfg.offline_albums = False
    assert as_family.get("/api/status").get_json()["moments"]["offline_albums"] is False
