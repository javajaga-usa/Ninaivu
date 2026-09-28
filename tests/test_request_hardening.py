"""Malformed requests are the caller's mistake, and are answered as one.

Three things this pins down. A JSON body that is not an object — ``[1]``,
``"on"``, ``5`` — used to reach ``data.get(...)`` in some fifty endpoints and
come back 500. A family member's request that mixed their own favourite with a
tag only an admin may set was refused with 403 *after* the favourite had been
saved. And the one password check with no limit on it — the current password
asked for when changing it — now has one.
"""

import ast
import inspect
import textwrap

import pytest

from ninaivu.api import accounts_api


def _calls_json_object(view) -> bool:
    """Whether this view reads its body through the shared helper."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(view))))
    except (OSError, TypeError):
        return False
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "json_object" for node in ast.walk(tree))


def test_every_body_reading_endpoint_refuses_a_body_that_is_not_an_object(app, as_admin):
    from werkzeug.routing import IntegerConverter
    urls = app.url_map.bind("localhost")
    routes = [rule for rule in app.url_map.iter_rules()
              if "POST" in rule.methods
              and _calls_json_object(app.view_functions[rule.endpoint])]
    assert len(routes) > 30, "the helper should be in use across the API"
    for rule in routes:
        values = {name: 1 if isinstance(converter, IntegerConverter) else "x"
                  for name, converter in rule._converters.items()}
        path = urls.build(rule.endpoint, values, method="POST")
        for body in ([1], "on", 5):
            response = as_admin.post(path, json=body)
            assert 400 <= response.status_code < 500, (path, body, response.status_code)


@pytest.mark.parametrize("path", ["/api/asset/{id}", "/api/assets/bulk"])
def test_a_family_member_sending_a_list_gets_a_400(as_family, path):
    asset_id = as_family.get("/api/assets?limit=1").get_json()["items"][0]["id"]
    for body in ([1], "favorite", 5):
        response = as_family.post(path.format(id=asset_id), json=body)
        assert response.status_code == 400, (body, response.status_code)


def test_a_refused_change_saves_nothing(as_family):
    item = as_family.get("/api/assets?limit=1").get_json()["items"][0]
    assert not item["favorite"]
    response = as_family.post(f"/api/asset/{item['id']}",
                              json={"favorite": True, "tags": ["mine"]})
    assert response.status_code == 403
    assert not as_family.get(f"/api/asset/{item['id']}").get_json()["favorite"]


def test_a_family_member_may_still_favourite(as_family):
    item = as_family.get("/api/assets?limit=1").get_json()["items"][0]
    response = as_family.post(f"/api/asset/{item['id']}", json={"favorite": True})
    assert response.status_code == 200
    assert response.get_json()["favorite"]


@pytest.mark.parametrize("colour, ok", [
    ("#4aa8ff", True), ("#ABC", True), ("", True),
    ("#zzzzzz", False), ("#12345", False), ("#1234567", False), ("red", False),
    ("#12 45", False),
])
def test_a_profile_colour_is_a_hex_colour(as_family, colour, ok):
    response = as_family.post("/api/me", json={"color": colour})
    assert (response.status_code == 200) is ok, response.get_json()


@pytest.fixture()
def clean_attempts():
    accounts_api._ATTEMPTS.clear()
    yield
    accounts_api._ATTEMPTS.clear()


def test_guessing_the_current_password_is_limited(clean_attempts, as_family):
    from conftest import FAMILY
    for _ in range(accounts_api._MAX_ATTEMPTS):
        wrong = as_family.post("/api/me/password",
                               json={"current": "not-it-at-all", "password": "brandnew-pass42"})
        assert wrong.status_code == 403
    # Past the limit, even the right one waits: otherwise the limit only
    # slows down the wrong guesses and still answers the right one.
    blocked = as_family.post("/api/me/password",
                             json={"current": FAMILY[1], "password": "brandnew-pass42"})
    assert blocked.status_code == 429


def test_the_right_password_still_changes_it(clean_attempts, as_family, people):
    from conftest import FAMILY
    from ninaivu.server import auth
    response = as_family.post("/api/me/password",
                              json={"current": FAMILY[1], "password": "brandnew-pass42"})
    assert response.status_code == 200, response.get_json()
    assert auth.authenticate(people["conn"], FAMILY[0], "brandnew-pass42") is not None
