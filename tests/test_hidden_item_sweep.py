"""No family-app address hands a family member an item they may not see.

The gate is held route by route elsewhere (test_visibility_gate.py,
test_visibility_safety.py). This walks every GET address that takes one
number, gives it the id of a photograph the administrator has hidden or marked
explicit, and asks as a family member. A new route that forgets the gate
fails here on the day it is added, instead of being found by an audit.
"""

from __future__ import annotations

import pytest
from werkzeug.routing import IntegerConverter

from conftest import FAMILY, login

FOLDER = "private"

#: Addresses that may answer 200 for an item the caller cannot see, and why.
ALLOWED = {
    # Only whether the caller themself sent it for screening: a family member
    # who sent one has it held from them and still has to see it was sent.
    "/api/screening/<int:asset_id>",
}


def _one_number_routes(app):
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or len(rule._converters) != 1:
            continue
        (name, converter), = rule._converters.items()
        if isinstance(converter, IntegerConverter):
            yield rule, name


@pytest.mark.parametrize("mark", ["hidden", "explicit"])
def test_no_address_opens_an_item_the_family_may_not_see(as_admin, app, scanned, mark):
    _, conn, _ = scanned
    listing = as_admin.get(f"/api/assets?limit=200&folder={FOLDER}").get_json()
    secret = listing["items"][0]["id"]
    family = login(app.test_client(), *FAMILY)
    assert family.get(f"/api/asset/{secret}").status_code == 200, "starts out visible"
    if mark == "hidden":
        answer = as_admin.post("/api/visibility", json={"ids": [secret], "visibility": mark})
        assert answer.status_code == 200, answer.get_data(as_text=True)
    else:
        # Adult content is marked by the screening pass or by an administrator
        # deciding on the screening queue; either way it lands here.
        conn.execute("UPDATE assets SET nsfw = 1 WHERE id = ?", (secret,))
        conn.commit()
    filename = conn.execute("SELECT filename FROM assets WHERE id = ?", (secret,)).fetchone()[0]

    urls = app.url_map.bind("localhost")
    routes = list(_one_number_routes(app))
    assert len(routes) > 30, "the sweep should reach the item routes"
    opened = []
    for rule, name in routes:
        if rule.rule in ALLOWED:
            continue
        path = urls.build(rule.endpoint, {name: secret}, method="GET")
        response = family.get(path)
        if response.status_code < 400 or filename.encode() in response.get_data():
            opened.append(f"{path} -> {response.status_code}")
    assert not opened, f"a {mark} item reached a family member:\n" + "\n".join(opened)


def test_the_administrator_still_opens_it(as_admin, scanned):
    listing = as_admin.get(f"/api/assets?limit=200&folder={FOLDER}").get_json()
    secret = listing["items"][0]["id"]
    as_admin.post("/api/visibility", json={"ids": [secret], "visibility": "hidden"})
    assert as_admin.get(f"/api/asset/{secret}").status_code == 200
    assert as_admin.get(f"/api/thumb/{secret}").status_code == 200
