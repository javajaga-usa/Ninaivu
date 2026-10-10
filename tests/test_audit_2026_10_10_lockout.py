"""Audit of 10 October 2026: guesses from the internet spend their own
account-wide allowance, so they cannot lock the household out at home, and
are still limited."""

import pytest

from conftest import ADMIN
from ninaivu.server import auth


def _outside(i):
    return dict(base_url="https://photos.example.org",
                environ_base={"REMOTE_ADDR": f"81.2.69.{10 + i}"})


HOME = dict(environ_base={"REMOTE_ADDR": "192.168.1.20"})


@pytest.fixture()
def outside(cfg):
    cfg.allowed_hosts = ["photos.example.org"]


def test_wrong_pins_from_the_internet_do_not_lock_the_profile_at_home(
        app, people, scanned, outside):
    _, conn, _ = scanned
    fam = people["family"].id
    auth.set_pin(conn, fam, "4827")
    codes = [app.test_client().post("/api/auth/enter", json={"id": fam, "secret": f"{1000 + i}"},
                                    **_outside(i)).status_code for i in range(25)]
    assert codes[-1] == 429                       # still limited from out there
    home = app.test_client().post("/api/auth/enter", json={"id": fam, "secret": "4827"}, **HOME)
    assert home.status_code == 200, home.get_json()


def test_taps_from_the_internet_on_an_open_profile_spend_nothing(app, people, scanned, outside):
    _, conn, _ = scanned
    fam = people["family"].id
    conn.execute("UPDATE users SET password=NULL, pin=NULL WHERE id=?", (fam,))
    conn.commit()
    codes = {app.test_client().post("/api/auth/enter", json={"id": fam},
                                    **_outside(i)).status_code for i in range(25)}
    assert codes == {403}
    home = app.test_client().post("/api/auth/enter", json={"id": fam}, **HOME)
    assert home.status_code == 200, home.get_json()


def test_wrong_passwords_from_the_internet_do_not_lock_the_admin_out_at_home(
        app, people, scanned, outside):
    codes = [app.test_client().post("/api/auth/login", json={
        "username": ADMIN[0], "password": f"wrong{i}xx"}, **_outside(i)).status_code
        for i in range(25)]
    assert codes[-1] == 429
    phone = app.test_client().post("/api/auth/login",
                                   json={"username": ADMIN[0], "password": ADMIN[1]}, **HOME)
    assert phone.status_code == 200, phone.get_json()
