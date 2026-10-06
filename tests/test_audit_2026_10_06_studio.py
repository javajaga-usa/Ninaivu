"""The Creative Studio's console, from the audit of 6 October 2026 (A-45):
its two settings routes read a body without a length whole."""

from __future__ import annotations

import io
import json

import pytest

from conftest import ADMIN, login
from ninaivu import extensions

pytest.importorskip("ninaivu_studio")


@pytest.fixture(autouse=True)
def studio_on(cfg, monkeypatch):
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_studio")
    extensions.discover(refresh=True)
    cfg.extensions = ["creative-studio"]
    yield
    extensions.discover(refresh=True)


@pytest.mark.parametrize("path", ["/api/admin/ai-server", "/api/admin/ai-server/workflows"])
def test_a_settings_body_without_a_length_is_held_to_its_limit(app, people, path):
    admin = login(app.test_client(), *ADMIN)
    big = json.dumps({"name": "x" * (5 * 1024 * 1024)}).encode()
    answer = admin.post(path, input_stream=io.BytesIO(big), content_type="application/json",
                        environ_overrides={"wsgi.input_terminated": True})
    assert answer.status_code == 413
