"""The first-day walk-through: opens once, ticks off what is already done."""

from conftest import ADMIN, FAMILY, login
from ninaivu.server.config import Config


def test_a_new_library_has_its_first_day_ahead(app, people):
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    state = admin.get("/api/admin/first-day").get_json()
    assert state["done"] is False
    assert state["library"]["chosen"] is True and state["library"]["root"]
    assert state["people"] == 2                     # Maya and the neighbour
    assert set(state["ai"]) == {"faces_enabled", "place_names", "ocr_enabled"}
    assert "enabled" in state["backup"]


def test_finishing_it_is_remembered_across_a_restart(app, people, monkeypatch):
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    assert admin.post("/api/admin/first-day", json={}).get_json() == {"done": True}
    assert admin.get("/api/admin/first-day").get_json()["done"] is True
    cfg = app.config["MV_CONFIG"]
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(cfg.state_dir))
    assert Config.load().first_day_done is True


def test_only_an_administrator_has_a_first_day(app, people):
    family = login(app.test_client(), *FAMILY)
    family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    assert family.get("/api/admin/first-day").status_code in (401, 403, 404)
    assert family.post("/api/admin/first-day", json={}).status_code in (401, 403, 404)
