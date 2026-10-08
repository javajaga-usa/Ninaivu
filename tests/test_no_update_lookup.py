"""Ninaivu never asks the internet whether a newer version is out.

The household chose to run the version it has; a newer one arrives as an
installer somebody downloads and runs, with Ninaivu stopped first. The daily
GitHub check (server/updates.py), its console switch and the tray's *Check
for an update* are gone, and these keep them gone.
"""
import json
from pathlib import Path

from ninaivu import __version__
from ninaivu.server.config import Config

PACKAGE = Path(__file__).resolve().parents[1] / "ninaivu"


def test_nothing_in_the_program_asks_github_for_releases():
    found = []
    for path in PACKAGE.rglob("*"):
        if path.suffix not in {".py", ".js", ".html"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("api.github.com", "/releases/latest", "update_check"):
            if needle in text:
                found.append(f"{path.relative_to(PACKAGE)}: {needle}")
    assert not found, found
    assert not (PACKAGE / "server" / "updates.py").exists()


def test_a_settings_file_from_before_still_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"update_check": True, "house_name": "Kept"}))
    cfg = Config.load()
    assert cfg.house_name == "Kept"
    assert not hasattr(cfg, "update_check")


def test_the_server_page_shows_the_version_and_nothing_about_a_newer_one(app, people):
    from conftest import ADMIN, login
    admin = login(app.test_client(), *ADMIN)
    state = admin.get("/api/admin/server").get_json()
    assert state["version"] == __version__
    assert "update" not in state


def test_the_console_says_stop_before_updating(app):
    page = (PACKAGE / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "press Stop here first" in page
    assert 'id="sv-update-toggle"' not in page
