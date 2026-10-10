"""Turning Gemini on from the console, without the key ever coming back out.

Gemini was wired in and could not be switched on from anywhere: the only way to
give it a key was an environment variable or editing a settings file by hand.
The console takes one now. What it must never do is hand it back — not to the
page, not into the audit log — so these check the absence as carefully as the
presence.
"""

import json
import urllib.error

import pytest

from conftest import FAMILY, GUEST, login
from ninaivu.media import model_catalog

# The extension package, or nothing here runs: the Gemini code is no longer in
# the core. ``pip install -e extensions/gemini``, or set NINAIVU_EXTENSION_MODULES
# with extensions/gemini on the path, as CI does.
gemini_media = pytest.importorskip("ninaivu_gemini.gemini")


@pytest.fixture()
def cfg(cfg, monkeypatch):
    """The core's config with the Gemini extension switched on."""
    from ninaivu import extensions
    monkeypatch.setenv(extensions.DEV_MODULES_VAR, "ninaivu_gemini")
    extensions.discover(refresh=True)
    cfg.extensions = ["gemini"]
    return cfg

KEY = "AIzaSyD-example-key-1234567890abcdWXYZ"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test here may reach Google, whatever it forgets to patch.

    An early version of one of these unset the wrong thing, took the SDK path
    instead of the one it meant to test, and sent a real request to Google
    with a made-up key. The SDK is made unimportable and the plain HTTP path
    refuses outright, so a test that needs either has to replace it on purpose.
    """
    import sys

    monkeypatch.setitem(sys.modules, "google.genai", None)

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(gemini_media.urllib.request, "urlopen", refuse)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """A state folder and a models folder of its own, no key in the
    environment, and no network. The state folder is the one the ``cfg``
    fixture gives the app, so a key saved by a route and one read directly
    are the same key."""
    root = tmp_path / ".ai-models"
    monkeypatch.setattr(model_catalog, "models_root", lambda: root)
    monkeypatch.setattr(gemini_media, "state_dir", lambda: tmp_path / "state")
    for name in gemini_media._ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gemini_media, "check_api_key", lambda key: (True, ""))
    return root


# --- the key is kept, and not given back ------------------------------------

def test_a_saved_key_turns_gemini_on(isolated):
    gemini_media.save_api_key(KEY)
    assert gemini_media.key_status()["set"] is True


def test_only_the_last_four_characters_are_ever_shown(isolated):
    gemini_media.save_api_key(KEY)
    status = gemini_media.key_status()
    assert status["hint"] == "…WXYZ"
    assert KEY not in json.dumps(status)


def test_the_console_never_sends_the_key_back(isolated, as_admin):
    response = as_admin.post("/api/admin/gemini", json={"key": KEY})
    assert response.status_code == 200
    assert KEY not in response.get_data(as_text=True)
    assert KEY not in as_admin.get("/api/admin/gemini").get_data(as_text=True)


def test_the_audit_log_says_what_happened_but_not_what_the_key_was(
        isolated, as_admin, scanned):
    _, conn, _ = scanned
    as_admin.post("/api/admin/gemini", json={"key": KEY})
    details = [r[0] for r in conn.execute("SELECT detail FROM audit")]
    assert any("Gemini" in (d or "") for d in details)
    assert not any(KEY in (d or "") for d in details)


def test_removing_it_turns_gemini_off(isolated):
    gemini_media.save_api_key(KEY)
    gemini_media.save_api_key("")
    assert gemini_media.key_status()["set"] is False


def test_the_models_settings_are_left_as_they_are(isolated):
    """The key used to share the file that holds where every model lives.
    It has a file of its own in the state folder now, and saving or removing
    it leaves the models' file untouched."""
    isolated.mkdir(parents=True)
    (isolated / "settings.json").write_text(
        json.dumps({"segmentation_model": "/models/rmbg.onnx"}))
    gemini_media.save_api_key(KEY)
    gemini_media.save_api_key("")
    stored = json.loads((isolated / "settings.json").read_text())
    assert stored == {"segmentation_model": "/models/rmbg.onnx"}
    assert KEY not in (isolated / "settings.json").read_text()


# --- a bad key is caught before anybody relies on it -------------------------

def test_a_key_google_refuses_is_not_kept(isolated, as_admin, monkeypatch):
    monkeypatch.setattr(gemini_media, "check_api_key",
                        lambda key: (False, "Google did not accept that key."))
    response = as_admin.post("/api/admin/gemini", json={"key": KEY})
    assert response.status_code == 400
    assert gemini_media.key_status()["set"] is False


@pytest.mark.parametrize("bad", ["has a space", "x" * 300])
def test_something_that_is_not_a_key_is_refused(isolated, bad):
    with pytest.raises(ValueError):
        gemini_media.save_api_key(bad)


# --- the environment wins, and says so ---------------------------------------

def test_a_key_in_the_environment_wins_and_is_not_overwritten(
        isolated, as_admin, monkeypatch):
    """An operator's key must not be quietly replaced by one typed into a page
    — and a key saved here would be written down and never used."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaEnvironmentKey9999")
    response = as_admin.post("/api/admin/gemini", json={"key": KEY})
    assert response.status_code == 409
    status = gemini_media.key_status()
    assert status["source"] == "environment"
    assert status["variable"] == "GEMINI_API_KEY"


def test_only_an_admin_can_set_it(app, people, isolated):
    for who in (FAMILY, GUEST):
        client = login(app.test_client(), *who)
        assert client.post("/api/admin/gemini",
                           json={"key": KEY}).status_code in (401, 403, 404)


# --- and it is never put in a URL --------------------------------------------

class _Recorder:
    def __init__(self, error=None):
        self.error = error
        self.request = None

    def __call__(self, request, timeout=None):
        self.request = request
        if self.error:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def test_checking_a_key_sends_it_in_a_header_not_the_url():
    """A URL is the part of a request that gets written down — by proxies, by
    anything logging what was fetched."""
    opener = _Recorder()
    assert gemini_media.check_api_key(KEY, opener=opener) == (True, "")
    assert KEY not in opener.request.full_url
    assert opener.request.get_header("X-goog-api-key") == KEY


def test_a_refused_key_is_reported_plainly():
    refused = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    ok, why = gemini_media.check_api_key(KEY, opener=_Recorder(refused))
    assert not ok and "did not accept" in why


def test_the_request_that_does_the_work_keeps_the_key_out_of_the_url(monkeypatch):
    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["header"] = request.get_header("X-goog-api-key")
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(gemini_media.urllib.request, "urlopen", opener)
    # The SDK is imported inside the function and, where it is installed,
    # is used instead of this path — so it has to be made unimportable. The
    # first version of this test only unset an attribute, took the SDK path,
    # and sent a real request to Google.
    import sys
    monkeypatch.setitem(sys.modules, "google.genai", None)
    # What an unreachable Google turns into, rather than any exception at all:
    # a blind match would also pass on the KeyError of a test that had broken.
    with pytest.raises(RuntimeError, match="Cannot reach"):
        gemini_media._call_interactions_api(KEY, "gemini-3.8-flash", [])
    assert KEY not in seen["url"]
    assert seen["header"] == KEY
