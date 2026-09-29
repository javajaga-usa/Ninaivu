"""Regressions for the product audit of 29 September 2026 (the findings a
stranger installing Ninaivu would have met)."""
import json
from pathlib import Path


from ninaivu.server.config import Config

ROOT = Path(__file__).resolve().parents[1]


# --- H6: the environment does not undo what the console chose ------------------

def test_a_console_choice_outlasts_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"open_browsing": False}))
    monkeypatch.setenv("NINAIVU_OPEN_BROWSING", "1")
    cfg = Config.load()
    assert cfg.open_browsing is False, "a private library stays private after a restart"


def test_the_environment_seeds_what_nobody_chose_and_is_not_written_down(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("NINAIVU_WATCH", "0")
    cfg = Config.load()
    assert cfg.watch is False
    cfg.save()
    assert "watch" not in json.loads((tmp_path / "config.json").read_text())
    monkeypatch.setenv("NINAIVU_WATCH", "1")
    assert Config.load().watch is True, "so a later environment still has its say"


def test_where_things_are_is_always_the_environments(tmp_path, monkeypatch):
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"ai_models_dir": "/old"}))
    monkeypatch.setenv("NINAIVU_AI_MODELS_DIR", "/models")
    assert Config.load().ai_models_dir == "/models"


def test_the_docker_env_file_only_names_variables_ninaivu_reads():
    source = (ROOT / "ninaivu" / "server" / "config.py").read_text(encoding="utf-8")
    source += (ROOT / "ninaivu" / "__main__.py").read_text(encoding="utf-8")
    compose_only = {"MEDIA_DIR", "HF_TOKEN"}
    for line in (ROOT / "installers" / "docker" / ".env.example").read_text().splitlines():
        line = line.strip().lstrip("# ").strip()
        if "=" not in line or not line.split("=")[0].isupper():
            continue
        name = line.split("=")[0]
        if name in compose_only or not name.startswith("NINAIVU_"):
            continue
        assert f'"{name}"' in source, f"{name} in .env.example is never read"


# --- H7: no random port when port 80 is not allowed ------------------------------

def test_without_permission_for_port_80_the_family_app_gets_8080(monkeypatch):
    from ninaivu import __main__ as main
    monkeypatch.setattr(main, "PREFERRED_WAIT", 0)
    monkeypatch.setattr(main, "port_is_free", lambda host, port: None if port < 1024 else True)
    assert main.pick_port("0.0.0.0", 80) == 8080
    assert main.pick_port("0.0.0.0", 443) == 8080
    monkeypatch.setattr(main, "port_is_free",
                        lambda host, port: None if port < 1024 else port != 8080)
    assert main.pick_port("0.0.0.0", 80) == 8081, "and the next fixed one if 8080 is taken"


# --- M1: a new install does not start with "something went wrong" ---------------

def test_the_setup_code_is_not_logged_as_a_warning():
    source = (ROOT / "ninaivu" / "__main__.py").read_text(encoding="utf-8")
    assert 'warning("first run: the setup code' not in source
    assert 'info("first run: the setup code is %s", code)' in source


# --- M6: the recovery commands are in the package -------------------------------

def test_recovery_is_a_ninaivu_command_not_a_tools_script(tmp_path, monkeypatch, capsys):
    from ninaivu import __main__ as main
    from ninaivu.cli import backup_restore, reroot
    seen = []
    monkeypatch.setattr(backup_restore, "main", lambda argv=None: seen.append(("br", argv)) or 0)
    monkeypatch.setattr(reroot, "main", lambda argv=None: seen.append(("rr", argv)) or 0)
    assert main.main(["restore", "x.tar.gz"]) == 0
    assert main.main(["list-backups"]) == 0
    assert main.main(["reroot", "--from", "/a", "--to", "/b"]) == 0
    assert seen == [("br", ["restore", "x.tar.gz"]), ("br", ["list"]),
                    ("rr", ["--from", "/a", "--to", "/b"])]
    for path in ("ninaivu/cloud/service.py", "ninaivu/static/js/admin.js"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "tools/backup_restore.py" not in text.replace("``tools/backup_restore.py``", "")


def test_the_tools_scripts_still_work_for_a_checkout():
    import tools.backup_restore as old
    from ninaivu.cli import backup_restore
    assert old is backup_restore


# --- M2: the keyboard help is translated too --------------------------------------

def test_every_line_of_the_keyboard_help_is_marked_for_translation():
    import re
    page = (ROOT / "ninaivu" / "templates" / "index.html").read_text(encoding="utf-8")
    start = page.index('id="help-modal"')
    block = page[start:page.index("</div>\n</div>", start)]
    assert re.findall(r"<dd>", block) == [], "a shortcut line without data-i18n"
    ta = json.loads((ROOT / "ninaivu" / "static" / "i18n" / "ta.json").read_text(encoding="utf-8"))
    for key in re.findall(r'data-i18n="([^"]+)"', block):
        import html
        assert html.unescape(key) in ta, f"no Tamil for {key!r}"
