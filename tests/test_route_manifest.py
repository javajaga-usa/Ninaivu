"""The route manifest (server/routes.py): every route says who may call it."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from flask import Blueprint, Flask

from ninaivu.server import auth, routes

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "tests" / "data" / "route_manifest.txt"


def _tool():
    spec = importlib.util.spec_from_file_location("route_manifest", ROOT / "tools" / "route_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built():
    return _tool().build()


def test_the_manifest_in_the_repository_is_current(built):
    assert FILE.read_text(encoding="utf-8") == built, \
        "tests/data/route_manifest.txt is out of date: run python tools/route_manifest.py " \
        "and read the changed lines: they are changes to who may call what"


def test_every_access_is_one_the_module_names(built):
    for line in built.splitlines()[2:]:
        access = line.split("|")[3].split()[0]
        assert access in routes.ACCESS, line


def test_console_only_blueprints_are_never_on_the_family_port(built):
    console_only = {"archive", "cloud", "migration", "server", "import", "drives", "ai_models",
                    "components", "api_admin", "admin_accounts", "admin", "mugil", "mugil_update"}
    home = [line for line in built.splitlines()[2:] if line.startswith("home ")]
    assert home
    blueprint = [line.rsplit("|", 1)[1].strip().split(".")[0] for line in home]
    assert sorted(set(blueprint) & console_only) == []


def test_a_route_that_says_nothing_stops_the_app():
    app = Flask(__name__)
    bp = Blueprint("forgotten", __name__)

    @bp.post("/api/forgotten")
    def forgotten():
        return "changed"

    app.register_blueprint(bp)
    with pytest.raises(routes.Undeclared, match="forgotten"):
        routes.check(app, "home")


def test_require_marks_the_view_with_the_stricter_role():
    @auth.require_admin
    @auth.require_family
    def both():
        return "x"

    @auth.require_family
    def family():
        return "x"

    assert both.ninaivu_access == "admin"
    assert family.ninaivu_access == "family"


def test_a_marked_route_declares_itself():
    app = Flask(__name__)
    bp = Blueprint("marked", __name__)

    @bp.post("/api/marked")
    @auth.require_admin
    def marked():
        return "x"

    app.register_blueprint(bp)
    routes.check(app, "admin")
    found, missing = routes.routes(app, "admin")
    assert missing == []
    (route,) = [r for r in found if r.endpoint == "marked.marked"]
    assert (route.access, route.writes, route.cross_origin) == ("admin", True, "same origin")


def test_the_phone_inbox_is_the_one_write_without_the_same_origin_rule(built):
    other = [line for line in built.splitlines()[2:]
             if "| writes |" in line and "same origin" not in line]
    assert other and all("/dav" in line for line in other)


def test_an_extension_route_that_says_nothing_is_kept_to_the_administrator():
    app = Flask(__name__)
    bp = Blueprint("ext", __name__)

    @bp.get("/api/ext/quiet")
    def quiet():
        return "x"

    @bp.get("/api/ext/open")
    @routes.declare("anyone", "a health check")
    def open_():
        return "x"

    app.register_blueprint(bp)
    closed = routes.keep_to_the_administrator(app, ["ext.quiet", "ext.open_"], "extension ext")
    assert closed == ["ext.quiet"]
    routes.check(app, "home")
    found = {r.endpoint: r for r in routes.routes(app, "home")[0]}
    assert found["ext.quiet"].access == "admin"
    assert (found["ext.open_"].access, found["ext.open_"].why) == ("anyone", "a health check")
    client = app.test_client()
    assert client.get("/api/ext/quiet").status_code == 401
    assert client.get("/api/ext/open").status_code == 200


def test_declare_takes_only_the_named_kinds_of_access():
    with pytest.raises(ValueError):
        routes.declare("everyone", "no such access")
