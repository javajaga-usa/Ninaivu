"""Extensions: off until switched on, and never the default for anything.

The core's promise is that a photograph never leaves the house unless the
administrator chose where it goes. These check the mechanism that keeps it:
an extension is found, is off by default, adds nothing until it is on, and
Sudar's own routes hand an edit to one only when the request names it.
"""

import sys
import types

import pytest

from conftest import ADMIN, FAMILY, login
from ninaivu import extensions


@pytest.fixture()
def fake_extension(monkeypatch):
    """A stand-in extension module, found through the development variable."""
    calls = {"register": []}
    module = types.ModuleType("ninaivu_fakeext")
    module.NAME = "fakeext"
    module.TITLE = "Fake"
    module.SUMMARY = "Sends nothing anywhere; for the tests."
    module.DATA_LEAVES_THE_MACHINE = True
    module.DESTINATION = "nowhere real"
    module.DOWNLOADS = ""

    def register(app, face):
        calls["register"].append(face)
        from flask import Blueprint, jsonify
        bp = Blueprint("fakeext", __name__)

        @bp.get("/api/fakeext/ping")
        def ping():
            return jsonify(ok=True)
        app.register_blueprint(bp)

    class Provider:
        def capabilities(self):
            return {"fake": True}

        def is_available(self):
            return True

        def plan_adjustments(self, prompt, current, image_bytes=None):
            calls["plan"] = prompt
            return {"adjustments": {}, "explanation": "planned outside"}

        def generate_image_edit(self, prompt, image_bytes, options=None):
            calls["generate"] = prompt
            return b"\x89PNG\r\n\x1a\n"

    module.register = register
    module.image_provider = Provider()
    monkeypatch.setitem(sys.modules, "ninaivu_fakeext", module)
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_fakeext")
    extensions.discover(refresh=True)
    yield calls
    extensions.discover(refresh=True)


def test_an_installed_extension_is_found_and_off(fake_extension, cfg):
    found = extensions.discover()
    assert "fakeext" in found and found["fakeext"].problems == []
    assert cfg.extensions == [], "every extension is off until somebody turns it on"
    assert extensions.active(cfg) == []


def test_an_extension_that_is_off_adds_nothing(fake_extension, app, people):
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/fakeext/ping").status_code == 404
    assert fake_extension["register"] == []


@pytest.fixture()
def cfg_on(cfg):
    cfg.extensions = ["fakeext"]
    return cfg


def test_an_extension_that_is_on_registers_on_the_app(fake_extension, cfg_on):
    from ninaivu import create_app
    app = create_app(cfg_on)
    try:
        assert fake_extension["register"] == ["home"]
        # Reachable (the route exists); what it answers is the extension's business.
        assert app.test_client().get("/api/fakeext/ping").status_code != 404
    finally:
        app.config["MV_SERVICES"].stop(timeout=5.0)


def test_sudar_never_sends_an_edit_outside_unless_the_request_names_it(fake_extension, cfg_on,
                                                                      monkeypatch):
    """The generate route used to fall through to Gemini whenever no local
    model was installed. Now a request that names no provider stays home —
    and with no local model, it says so rather than going anywhere."""
    import base64
    from ninaivu import create_app
    app = create_app(cfg_on)
    try:
        monkeypatch.delenv("NINAIVU_IMAGE_EDIT_MODEL", raising=False)
        from ninaivu.media import ai_editing
        monkeypatch.setattr(ai_editing, "local_setting", lambda name, default="": default)
        from ninaivu.server import auth
        from ninaivu.storage import db
        conn = db.connect(cfg_on.db_path)
        admin = auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
        auth.create_user(conn, FAMILY[0], FAMILY[1], display_name="Maya",
                         role=auth.ROLE_FAMILY, created_by=admin.id)
        family = login(app.test_client(), *FAMILY)
        family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
        image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()

        unnamed = family.post("/api/ai-playground/generate",
                              json={"prompt": "warmer", "image": image})
        assert unnamed.status_code == 503, unnamed.get_data(as_text=True)
        assert "generate" not in fake_extension, "an unnamed request went to the extension"

        named = family.post("/api/ai-playground/generate",
                            json={"prompt": "warmer", "image": image, "provider": "fakeext"})
        assert named.status_code == 200 and fake_extension["generate"] == "warmer"

        planned = family.post("/api/ai-playground/plan",
                              json={"prompt": "warmer", "current": {}, "provider": "fakeext"})
        assert planned.status_code == 200 and fake_extension["plan"] == "warmer"

        unknown = family.post("/api/ai-playground/plan",
                              json={"prompt": "warmer", "current": {}, "provider": "elsewhere"})
        assert unknown.status_code == 404
    finally:
        app.config["MV_SERVICES"].stop(timeout=5.0)


def test_a_provider_that_is_off_is_not_reachable_by_name(fake_extension, app, people):
    """Installed but switched off: naming it is a 404, not a send."""
    import base64
    family = login(app.test_client(), *FAMILY)
    family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
    answer = family.post("/api/ai-playground/generate",
                         json={"prompt": "warmer", "image": image, "provider": "fakeext"})
    assert answer.status_code == 404
    assert "generate" not in fake_extension


def test_the_console_lists_extensions_and_says_what_leaves(fake_extension, app, people):
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    listing = admin.get("/api/admin/extensions").get_json()
    [item] = [e for e in listing["extensions"] if e["name"] == "fakeext"]
    assert item["enabled"] is False
    assert item["data_leaves_the_machine"] is True and item["destination"] == "nowhere real"
    assert listing["active"] == []


def test_switching_an_extension_on_is_saved_and_says_restart(fake_extension, app, people):
    from ninaivu.server.config import Config
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    answer = admin.post("/api/admin/extensions", json={"name": "fakeext", "enabled": True})
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["restart_needed"] is True
    cfg = app.config["MV_CONFIG"]
    assert cfg.extensions == ["fakeext"]
    import os
    os.environ["NINAIVU_STATE_DIR"] = str(cfg.state_dir)
    try:
        assert Config.load().extensions == ["fakeext"], "the switch was not saved"
    finally:
        del os.environ["NINAIVU_STATE_DIR"]
    # Not registered on the running app — that is what the restart is for.
    assert admin.get("/api/fakeext/ping").status_code == 404
    off = admin.post("/api/admin/extensions", json={"name": "fakeext", "enabled": False})
    assert off.get_json()["extensions"][0]["enabled"] is False


def test_an_extension_that_is_not_installed_cannot_be_switched_on(app, people):
    admin = login(app.test_client(), *ADMIN)
    admin.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    answer = admin.post("/api/admin/extensions", json={"name": "nothing", "enabled": True})
    assert answer.status_code == 404


def test_only_an_admin_sees_or_switches_extensions(fake_extension, app, people):
    family = login(app.test_client(), *FAMILY)
    family.environ_base["HTTP_SEC_FETCH_SITE"] = "same-origin"
    assert family.get("/api/admin/extensions").status_code in (401, 403, 404)
    assert family.post("/api/admin/extensions",
                       json={"name": "fakeext", "enabled": True}).status_code in (401, 403, 404)


# -- the Overview's "Needs you": the queues counted in one request -------------

def test_needs_you_counts_the_queues(app, people):
    admin = login(app.test_client(), *ADMIN)
    conn = people["conn"]
    first = conn.execute("SELECT id FROM assets ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute("INSERT INTO orientation_proposals(asset_id, rotation, confidence, status) "
                 "VALUES (?, 90, 0.9, 'pending')", (first,))
    conn.commit()
    data = admin.get("/api/admin/attention").get_json()
    by_key = {item["key"]: item for item in data["items"]}
    assert set(by_key) == {"uploads", "faces", "straighten", "problems"}
    assert by_key["straighten"]["count"] == 1 and by_key["straighten"]["page"] == "straighten"
    assert by_key["uploads"]["count"] == 0
    assert data["total"] == sum(i["count"] for i in data["items"])
    for item in data["items"]:
        assert item["title"] and item["detail"] and item["page"]


def test_needs_you_is_for_administrators(app, people):
    family = login(app.test_client(), *FAMILY)
    assert family.get("/api/admin/attention").status_code in (401, 403, 404)


# ---------------------------------------------------------------------------
# The core without Creative Studio
# ---------------------------------------------------------------------------

def test_without_creative_studio_sudar_still_answers(app, people):
    """Generative edits say what is missing; the rest of the Playground works
    with what the core carries."""
    import base64
    import io

    from PIL import Image

    from conftest import FAMILY, login

    assert extensions.studio() is None
    family = login(app.test_client(), *FAMILY)
    caps = family.get("/api/ai-playground/capabilities").get_json()
    assert caps["image_provider"] is None and caps["server_jobs"] == []
    assert caps["object_removal_provider"] in ("local", "local-ai")

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(buf, "PNG")
    png = base64.b64encode(buf.getvalue()).decode()
    answer = family.post("/api/ai-playground/generate", json={"prompt": "add snow", "image": png})
    assert answer.status_code == 503
    assert "Creative Studio" in answer.get_json()["error"]

    # Object removal falls back to the small local remover.
    mask_buf = io.BytesIO()
    mask = Image.new("L", (32, 32), 0)
    mask.paste(255, (8, 8, 20, 20))
    mask.save(mask_buf, "PNG")
    removed = family.post("/api/ai-playground/inpaint",
                          json={"image": png, "mask": base64.b64encode(mask_buf.getvalue()).decode()})
    assert removed.status_code == 200 and removed.mimetype == "image/png"

    # A server job with nothing to run it is a plain 404, not an error page.
    job = family.post("/api/ai-playground/server-jobs", json={"kind": "edit", "image": png, "prompt": "x"})
    assert job.status_code == 404


def test_the_ai_server_page_is_not_in_the_console_without_the_extension(app, people):
    from conftest import ADMIN, login

    from ninaivu import build_services, create_admin_app

    cfg = app.config["MV_CONFIG"]
    services = build_services(cfg)
    services.scanner.stop()
    admin = login(create_admin_app(services).test_client(), *ADMIN)
    assert admin.get("/api/admin/ai-server").status_code == 404
    page = admin.get("/").get_data(as_text=True)
    assert 'data-tab="ai-server" data-group="ai" data-needs-extension="creative-studio"' in page
