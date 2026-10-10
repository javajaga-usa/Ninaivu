"""Regressions for the web findings of the 10 October 2026 security audit.

Three of them, each the attack or the gap the audit named, run against the fix:

* L2 — a cross-origin *form* from an engine that predates fetch metadata
  (Firefox before 70, some embedded WebViews) arrives with the cookie and
  neither ``Sec-Fetch-Site`` nor ``Origin``. ``_refuse_cross_origin_writes``
  let such a request through, which was fine for the JSON routes (a form
  cannot send ``application/json``) and not for the ones that take a file.
  Those now want ``X-Requested-With`` when nothing else says where the
  request came from.
* Info — ``frame-ancestors 'none'`` without ``X-Frame-Options``.
* M4 — the launcher installed ``>=`` bounds straight from PyPI; it now
  installs under ``requirements/constraints-tested.txt`` when the checkout
  has it, and notices when that file changes.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from PIL import Image

from conftest import ADMIN, FAMILY, login

ROOT = Path(__file__).resolve().parents[1]

REFUSED = {"error": "Cross-origin request refused.", "status": 403}
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 400


def bare(app, who):
    """A signed-in client that sends what an old browser's cross-origin form
    sends: the cookie and nothing that says where it came from. The suite's
    own client (conftest.PageClient) carries ``X-Requested-With`` on every
    request, as the page's fetch helpers do, which is exactly what this must
    not."""
    return login(FlaskClient(app, app.response_class), *who)


def photo():
    out = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(out, "PNG")
    return out.getvalue()


def upload(client, **headers):
    return client.post("/api/upload", data={"file": (io.BytesIO(photo()), "photo.png")},
                       content_type="multipart/form-data", headers=headers)


def tell(client, asset_id, **headers):
    form = {"audio": (io.BytesIO(WEBM), "story.webm", "audio/webm;codecs=opus"),
            "duration": "12.5"}
    return client.post(f"/api/asset/{asset_id}/stories", data=form,
                       content_type="multipart/form-data", headers=headers)


# ---------------------------------------------------------------------------
# L2. A headerless form post with a file in it
# ---------------------------------------------------------------------------

def test_a_headerless_multipart_upload_is_refused(app, people):
    response = upload(bare(app, FAMILY))
    assert response.status_code == 403
    assert response.get_json() == REFUSED


def test_a_headerless_voice_story_is_refused(app, people):
    client = bare(app, FAMILY)
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    response = tell(client, asset_id)
    assert response.status_code == 403
    assert response.get_json() == REFUSED


def test_a_headerless_urlencoded_form_is_refused_too(app, people):
    """The other body a form can send. The upload route would answer 400 to
    it; the point is that it is refused before any route sees it."""
    response = bare(app, FAMILY).post("/api/upload", data={"file": "x"})
    assert response.status_code == 403
    assert response.get_json() == REFUSED


def test_the_pages_own_header_lets_the_upload_through(app, people):
    response = upload(bare(app, FAMILY), **{"X-Requested-With": "fetch"})
    assert response.status_code == 200, response.get_json()
    assert response.get_json().get("uploaded")


def test_fetch_metadata_lets_the_upload_through(app, people):
    response = upload(bare(app, FAMILY), **{"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200, response.get_json()


def test_the_pages_own_header_lets_a_story_through(app, people):
    client = bare(app, FAMILY)
    asset_id = client.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    response = tell(client, asset_id, **{"X-Requested-With": "fetch"})
    assert response.status_code == 201, response.get_json()


def test_a_headerless_json_post_still_works(app, people):
    """stop.bat, scripts, the test client: JSON, which no form can send."""
    response = bare(app, ADMIN).post("/api/scan/stop", json={})
    assert response.status_code == 200


def test_a_headerless_bodyless_post_still_works(app, people):
    """``POST /api/scan/stop`` with nothing at all, as a script sends it."""
    response = bare(app, ADMIN).post("/api/scan/stop")
    assert response.status_code == 200


def test_a_cross_site_request_is_still_refused_whatever_else_it_sends(app, people):
    """The custom header does not outrank the browser's own word."""
    response = upload(bare(app, FAMILY), **{"Sec-Fetch-Site": "cross-site",
                                            "X-Requested-With": "fetch"})
    assert response.status_code == 403
    assert response.get_json() == REFUSED


def test_the_suites_client_stands_in_for_the_page(client):
    """conftest.PageClient sends the header, so every existing upload test
    reads as the page's own fetch and not as the old browser's form."""
    assert client.environ_base["HTTP_X_REQUESTED_WITH"] == "fetch"


# ---------------------------------------------------------------------------
# Info. X-Frame-Options beside frame-ancestors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/api/assets?limit=1", "/api/asset/424242"])
def test_nothing_may_be_framed(client, path):
    response = client.get(path)
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# M4. The launcher installs under the tested constraints
# ---------------------------------------------------------------------------

@pytest.fixture()
def launcher(tmp_path, monkeypatch):
    """start.py loaded by path, as test_from_lite does, with a pip that only
    writes down what it was asked."""
    spec = importlib.util.spec_from_file_location(
        "audit_launcher", ROOT / "launcher" / "start.py")
    start = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start)
    asked = []

    class Done:
        returncode = 0

    monkeypatch.setattr(start.subprocess, "run",
                        lambda command, **kwargs: asked.append(list(command)) or Done())
    monkeypatch.setattr(start, "CONSTRAINTS", tmp_path / "constraints-tested.txt")
    return start, asked


def test_the_launcher_installs_under_the_checkouts_constraints(launcher, tmp_path):
    start, asked = launcher
    start.CONSTRAINTS.write_text("pillow==12.3.0\n", encoding="utf-8")
    assert start.pip_install(Path("python"), ["pillow>=12.3"], "core")
    (command,) = asked
    assert command[-2:] == ["-c", str(start.CONSTRAINTS)]
    assert "pillow>=12.3" in command, "the bound stays; the constraints pin it"


def test_without_the_file_the_launcher_asks_for_the_bounds_alone(launcher):
    start, asked = launcher
    assert not start.CONSTRAINTS.exists()
    start.pip_install(Path("python"), ["pillow>=12.3"], "core")
    (command,) = asked
    assert "-c" not in command


def test_the_torch_step_is_not_constrained(launcher):
    """The CPU index serves torch's dependencies at its own versions; a pin
    it cannot meet would fail the install over to the CUDA build."""
    start, asked = launcher
    start.CONSTRAINTS.write_text("fsspec==2026.1.0\n", encoding="utf-8")
    start.pip_install(Path("python"), ["torch"], "PyTorch (CPU build)",
                      ["--index-url", "https://download.pytorch.org/whl/cpu"],
                      constrained=False)
    (command,) = asked
    assert "-c" not in command and "--index-url" in command


def test_the_real_checkout_has_the_file_the_launcher_looks_for():
    assert (ROOT / "requirements" / "constraints-tested.txt").is_file()
    text = (ROOT / "requirements" / "constraints-tested.txt").read_text(encoding="utf-8")
    # torch itself is left to the CPU index; only OpenCLIP is pinned.
    assert "open_clip_torch==" in text
    assert not any(line.startswith("torch==") for line in text.splitlines())


def test_a_raised_pin_changes_the_refresh_stamp(launcher, tmp_path, monkeypatch):
    """refresh_if_changed keys on this stamp, so a bumped pin reaches an
    existing .venv the way a moved bound does."""
    start, asked = launcher
    without = start.requirements_stamp()
    start.CONSTRAINTS.write_text("pillow==12.3.0\n", encoding="utf-8")
    first = start.requirements_stamp()
    start.CONSTRAINTS.write_text("pillow==12.4.0\n", encoding="utf-8")
    second = start.requirements_stamp()
    assert len({without, first, second}) == 3
    assert start.requirements_stamp() == second, "and it is stable between reads"

    monkeypatch.setattr(start, "VENV_DIR", tmp_path / ".venv")
    start.VENV_DIR.mkdir()
    python = start.venv_python(start.VENV_DIR)
    start.refresh_if_changed(python)
    assert len(asked) == 1 and asked[0][-2:] == ["-c", str(start.CONSTRAINTS)]
    start.refresh_if_changed(python)
    assert len(asked) == 1, "nothing changed, nothing asked"
    start.CONSTRAINTS.write_text("pillow==12.5.0\n", encoding="utf-8")
    start.refresh_if_changed(python)
    assert len(asked) == 2, "a raised pin is asked for again"
