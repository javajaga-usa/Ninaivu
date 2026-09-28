"""The passes a scan can make, switched on and off from the console.

Naming places, reading the words in photographs and finding faces are all off
by default, and until now none of them had a switch — only a start-up flag, or
config.json by hand. So a household had no way of knowing they existed. On the
library this was found on, 21,887 photographs carried coordinates and none had
ever been named, and a text reader that had just been installed was never
going to be asked to read anything.
"""

import pytest

from conftest import FAMILY, GUEST, login
from ninaivu.api.admin_api import SCAN_PASSES
from ninaivu.media import scanner as scanner_mod
from ninaivu.media.scanner import Scanner

PASSES = ["place_names", "ocr_enabled", "faces_enabled"]


def test_all_three_can_be_switched():
    assert set(SCAN_PASSES) == set(PASSES)


@pytest.mark.parametrize("key", PASSES)
def test_the_console_turns_it_on(cfg, as_admin, key):
    setattr(cfg, key, False)
    response = as_admin.post("/api/admin/settings", json={key: True})
    assert response.status_code == 200
    assert response.get_json()["settings"][key] is True
    assert getattr(cfg, key) is True


@pytest.mark.parametrize("key", PASSES)
def test_and_off_again(cfg, as_admin, key):
    setattr(cfg, key, True)
    as_admin.post("/api/admin/settings", json={key: False})
    assert getattr(cfg, key) is False


@pytest.mark.parametrize("key", PASSES)
def test_the_console_is_told_what_it_is(cfg, as_admin, key):
    setattr(cfg, key, True)
    app = as_admin.get("/api/admin/overview").get_json()["app"]
    assert app[key] is True


@pytest.mark.parametrize("key", PASSES)
def test_it_survives_a_restart(cfg, as_admin, key):
    """Written to config.json, so it is still on after Ninaivu restarts —
    which it would not have been before `save()` stopped keeping a list."""
    import json
    as_admin.post("/api/admin/settings", json={key: True})
    stored = json.loads((cfg.state_dir / "config.json").read_text("utf-8"))
    assert stored[key] is True


def test_only_an_admin_can_switch_them(app, people):
    for who in (FAMILY, GUEST):
        client = login(app.test_client(), *who)
        response = client.post("/api/admin/settings",
                               json={"faces_enabled": True})
        assert response.status_code in (401, 403, 404)


# --- and a pass that is running stops when switched off ----------------------

def test_the_text_reader_stops_part_way_when_switched_off(scanned, monkeypatch):
    """Reading every photograph once is hours. Turning it off in the middle of
    those hours has to mean now, not the next scan."""
    cfg, conn, _ = scanned
    cfg.ocr_enabled = True
    cfg.ai_enabled = False
    read: list[str] = []

    from ninaivu.media import ocr

    def reads(path, **_):
        read.append(str(path))
        cfg.ocr_enabled = False               # somebody flips the switch
        return {"text": ""}

    monkeypatch.setattr(ocr, "available", lambda: True, raising=False)
    monkeypatch.setattr(ocr, "read", reads)
    scanner = Scanner(cfg)
    scanner._read_text(conn, cfg.active_root)

    # Exactly one: zero would mean the pass never ran and this proved nothing.
    assert len(read) == 1, f"read {len(read)} photographs"


def test_the_face_pass_is_told_to_stop_when_switched_off(scanned, monkeypatch):
    cfg, conn, _ = scanned
    cfg.faces_enabled = True
    seen: dict = {}

    class Indexer:
        class engine:
            available = True

        def detect_pass(self, conn, root, *, should_stop, on_progress):
            seen["before"] = should_stop()
            cfg.faces_enabled = False
            seen["after"] = should_stop()
            return {"ok": True, "scanned": 0, "faces": 0}

        def regroup(self, conn, root):
            pass

    scanner = Scanner(cfg)
    monkeypatch.setattr(scanner, "_face_indexer", lambda: Indexer())
    monkeypatch.setattr(scanner_mod.db, "assets_needing_faces",
                        lambda *a, **k: [{"id": 1}])
    scanner._find_faces(conn, cfg.active_root)

    assert seen == {"before": False, "after": True}
