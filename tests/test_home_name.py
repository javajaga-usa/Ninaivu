"""What the household calls this library.

Two names, and the relationship between them is the whole feature. The admin
sets one for the house; anybody in the family may keep their own instead. The
personal one is shown only to the person who set it, which is what makes it
safe to let a child set it to something silly — nobody else has to live with
the choice.
"""

import pytest

from conftest import ADMIN, FAMILY, GUEST, login
from ninaivu.server import auth
from ninaivu.server.config import Config, clean_home_name, home_name_for, house_name


# --- the resolution chain --------------------------------------------------

def test_an_unnamed_library_is_called_ninaivu():
    assert house_name(Config()) == "Ninaivu"


def test_a_member_without_a_name_of_their_own_sees_the_households():
    cfg = Config()
    cfg.house_name = "The Kumar Home"
    member = auth.User(id=1, username="maya", display_name="Maya",
                       role="family", active=True)
    assert home_name_for(member, cfg) == "The Kumar Home"


def test_a_members_own_name_wins_for_them():
    cfg = Config()
    cfg.house_name = "The Kumar Home"
    member = auth.User(id=1, username="maya", display_name="Maya",
                       role="family", active=True, home_label="Casa Chaos")
    assert home_name_for(member, cfg) == "Casa Chaos"


def test_clearing_a_personal_name_falls_back_rather_than_going_blank():
    cfg = Config()
    cfg.house_name = "The Kumar Home"
    member = auth.User(id=1, username="maya", display_name="Maya",
                       role="family", active=True, home_label="")
    assert home_name_for(member, cfg) == "The Kumar Home"


# --- what people can type --------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("  The  Kumar Home  ", "The Kumar Home"),
    ("Kumar\tHome", "Kumar Home"),      # a tab is a space, not nothing
    ("Fun\x00\x07Home", "FunHome"),             # control characters vanish
    ("Sağlam Ev", "Sağlam Ev"),                 # non-ASCII survives intact
    ("", ""),
    ("   ", ""),
    (None, ""),
])
def test_names_are_tidied_not_mangled(raw, expected):
    assert clean_home_name(raw) == expected


def test_a_name_cannot_push_the_rest_of_the_top_bar_off_a_phone():
    assert len(clean_home_name("x" * 500)) == 40


def test_markup_is_kept_as_literal_text():
    """Nothing here strips HTML, because nothing here renders HTML — every
    surface uses textContent. Silently mangling somebody's name would be the
    surprising behaviour, not the safe one."""
    assert clean_home_name("<b>Home</b>") == "<b>Home</b>"


# --- who may set what ------------------------------------------------------

def test_a_family_member_can_name_the_home_for_themselves(app, people):
    client = app.test_client()
    login(client, *FAMILY)

    response = client.post("/api/me", json={"home_label": "Casa Chaos"})
    assert response.status_code == 200
    assert response.get_json()["home_label"] == "Casa Chaos"

    assert client.get("/api/auth/state").get_json()["home_name"] == "Casa Chaos"


def test_one_persons_name_is_invisible_to_everyone_else(app, people):
    """The point of the feature. Maya renaming the house must not rename it
    for her father, or the fun name stops being safe to allow."""
    maya = app.test_client()
    login(maya, *FAMILY)
    maya.post("/api/me", json={"home_label": "Casa Chaos"})

    dad = app.test_client()
    login(dad, *ADMIN)
    assert dad.get("/api/auth/state").get_json()["home_name"] != "Casa Chaos"


def test_a_guest_cannot_rename_the_home(app, people):
    """A guest tile is usually shared and short-lived; renaming the house for
    the next visitor is a nuisance with nothing to recommend it."""
    client = app.test_client()
    login(client, *GUEST)
    assert client.post("/api/me", json={"home_label": "lol"}).status_code == 403


def test_clearing_it_returns_to_the_household_name(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    client.post("/api/me", json={"home_label": "Casa Chaos"})

    response = client.post("/api/me", json={"home_label": "   "})
    assert response.status_code == 200
    assert response.get_json()["home_label"] == ""


def test_only_an_admin_sets_the_household_name(app, people):
    for who in (FAMILY, GUEST):
        client = app.test_client()
        login(client, *who)
        response = client.post("/api/admin/settings", json={"house_name": "Nope"})
        assert response.status_code in (401, 403, 404)


def test_the_household_name_reaches_the_home_screen_icon(app, people, scanned):
    """The installed app's name can only ever be the household's: the manifest
    is fetched before anybody signs in, and one phone is shared by whoever
    picks it up."""
    cfg, _, _ = scanned
    client = app.test_client()
    login(client, *ADMIN)
    client.post("/api/admin/settings", json={"house_name": "The Kumar Home"})

    manifest = client.get("/manifest.webmanifest").get_json()
    assert manifest["name"] == "The Kumar Home"
    assert manifest["short_name"] == "The Kumar Home"


def test_a_personal_name_never_reaches_the_manifest(app, people):
    client = app.test_client()
    login(client, *FAMILY)
    client.post("/api/me", json={"home_label": "Casa Chaos"})
    assert client.get("/manifest.webmanifest").get_json()["name"] != "Casa Chaos"


def test_the_name_survives_a_restart(scanned, monkeypatch):
    """It is a setting, not a session: written to config.json on save.

    Loaded the way a real start loads it — through NINAIVU_STATE_DIR. Passing
    state_dir= to Config.load() would not do it: the stored file is read
    before keyword overrides are applied, so the override arrives too late to
    choose which file was read.
    """
    cfg, _, _ = scanned
    cfg.house_name = "The Kumar Home"
    cfg.save()

    monkeypatch.setenv("NINAIVU_STATE_DIR", str(cfg.state_dir))
    reloaded = Config.load()
    assert house_name(reloaded) == "The Kumar Home"
