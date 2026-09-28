"""The update check: one plain request a day, nothing sent, nothing installed."""

from ninaivu.server import updates
from ninaivu.server.config import Config


def test_versions_compare_as_numbers():
    assert updates.newer("v0.10.0", "0.9.1")
    assert not updates.newer("0.1.0", "0.1.0")
    assert not updates.newer("garbage", "0.1.0")


def test_a_newer_release_is_reported_with_its_page():
    result = updates.check("0.1.0", fetch=lambda: {"tag_name": "v0.2.0",
                                                   "html_url": "https://github.com/x/Ninaivu/releases/tag/v0.2.0"})
    assert result["available"] and result["latest"] == "0.2.0" and result["url"].endswith("v0.2.0")


def test_being_offline_is_not_an_error_anybody_sees():
    def down():
        raise OSError("no network")
    result = updates.check("0.1.0", fetch=down)
    assert result["available"] is False and "no network" in result["error"]


def test_the_check_sends_no_identifier(monkeypatch):
    seen = {}

    class Answer:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return b'{"tag_name": "v0.1.0"}'

    def urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        return Answer()

    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    updates.check("0.1.0")
    assert seen["url"] == updates.RELEASES_URL
    joined = " ".join(f"{k}={v}" for k, v in seen["headers"].items()).lower()
    assert "0.1.0" not in joined and "cookie" not in joined, seen["headers"]


def test_off_means_no_thread_at_all():
    cfg = Config(); cfg.update_check = False
    checker = updates.UpdateChecker(cfg, "0.1.0", fetch=lambda: {"tag_name": "v9.0.0"})
    checker.start()
    assert checker._thread is None
    assert checker.describe() == {"enabled": False, "current": "0.1.0"}


def test_the_console_can_switch_it_off(app, people):
    from conftest import ADMIN, login
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    answer = admin.post("/api/admin/settings", json={"update_check": False})
    assert answer.status_code == 200 and answer.get_json()["settings"]["update_check"] is False
    state = admin.get("/api/admin/server").get_json()
    assert state["update"]["enabled"] is False
