"""Every setting has a group, a meaning and a default, and the console's
Advanced settings page can read and change them without a hand-kept list."""

from dataclasses import fields

from conftest import ADMIN, login
from ninaivu import build_services, create_admin_app
from ninaivu.server import auth, settings_groups
from ninaivu.server.config import RUNTIME_ONLY, Config


def test_every_setting_is_in_exactly_one_group():
    names = {f.name for f in fields(Config)}
    grouped = [n for names_ in settings_groups.GROUPS.values() for n in names_]
    assert set(grouped) == names, (sorted(names - set(grouped)), sorted(set(grouped) - names))
    assert len(grouped) == len(set(grouped)), "a setting in two groups"
    assert list(settings_groups.GROUPS) == ["Library", "People", "Backup", "Remote access", "AI", "Advanced"]


def test_every_setting_says_what_it_means():
    described = settings_groups.describe(Config())
    silent = [s["name"] for g in described["groups"] for s in g["settings"] if not s["doc"]]
    assert silent == [], "add a #: comment above the field in config.py"


def test_the_first_screen_is_ten_settings_that_exist():
    names = {f.name for f in fields(Config)}
    assert len(settings_groups.FIRST_SCREEN) == 10
    assert set(settings_groups.FIRST_SCREEN) <= names
    assert not set(settings_groups.FIRST_SCREEN) & RUNTIME_ONLY


def test_runtime_settings_are_shown_under_advanced_and_marked():
    described = settings_groups.describe(Config())
    advanced = {s["name"]: s for s in next(g for g in described["groups"] if g["name"] == "Advanced")["settings"]}
    assert RUNTIME_ONLY <= set(advanced)
    assert all(advanced[n]["runtime"] for n in RUNTIME_ONLY)


def test_a_secret_is_reported_as_set_or_not_never_shown():
    cfg = Config()
    cfg.notify_smtp_password = "hunter2"
    described = settings_groups.describe(cfg)
    item = next(s for g in described["groups"] for s in g["settings"] if s["name"] == "notify_smtp_password")
    assert item["secret"] is True and item["value"] is True
    assert "hunter2" not in repr(described)


def test_values_are_coerced_to_the_type_the_field_holds():
    cfg = Config()
    changed = settings_groups.apply(cfg, {
        "thumb_quality": "70", "watch": "false", "ignore_dirs": "a\nb", "thumb_sizes": ["200", "400"],
        "cloud_rate_kbps": 0,
    })
    assert cfg.thumb_quality == 70 and cfg.watch is False
    assert cfg.ignore_dirs == {"a", "b"} or set(cfg.ignore_dirs) == {"a", "b"}
    assert tuple(cfg.thumb_sizes) == (200, 400)
    assert set(changed) == {"thumb_quality", "watch", "ignore_dirs", "thumb_sizes", "cloud_rate_kbps"} - (
        {"cloud_rate_kbps"} if Config().cloud_rate_kbps == 0 else set())


def test_one_bad_value_changes_nothing():
    cfg = Config()
    before = cfg.thumb_quality
    try:
        settings_groups.apply(cfg, {"thumb_quality": 70, "workers": "many"})
    except settings_groups.BadValue as exc:
        assert "workers" in str(exc)
    else:
        raise AssertionError("expected a BadValue")
    assert cfg.thumb_quality == before


def test_runtime_and_unknown_settings_are_refused():
    cfg = Config()
    for bad in ({"port": 9}, {"no_such_thing": 1}):
        try:
            settings_groups.apply(cfg, bad)
        except settings_groups.BadValue:
            continue
        raise AssertionError(f"{bad} should be refused")


def _console(scanned):
    cfg, conn, _ = scanned
    cfg.watch = False
    auth.bootstrap_admin(conn, ADMIN[0], ADMIN[1], "Dad")
    services = build_services(cfg)
    services.scanner.stop()
    return login(create_admin_app(services).test_client(), *ADMIN), cfg


def test_the_console_reads_and_changes_them(scanned):
    client, cfg = _console(scanned)
    body = client.get("/api/admin/settings/all").get_json()
    assert [g["name"] for g in body["groups"]][0] == "Library"
    assert body["first_screen"][0] == "house_name"

    response = client.post("/api/admin/settings/all", json={"settings": {"thumb_quality": 66, "workers": 3}})
    assert response.status_code == 200, response.get_json()
    assert set(response.get_json()["changed"]) == {"thumb_quality", "workers"}
    assert response.get_json()["restart"] == ["workers"], "workers only takes effect on the next start"
    assert cfg.thumb_quality == 66

    response = client.post("/api/admin/settings/all", json={"settings": {"port": 1}})
    assert response.status_code == 400

    page = client.get("/").get_data(as_text=True)
    assert 'data-tab="advanced"' in page and 'id="adv-groups"' in page
