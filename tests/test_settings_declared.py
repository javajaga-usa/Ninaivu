"""Settings declared once (config.setting): the variables, the two sets worked
out from the declarations, the layers, and the generated reference page."""
from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from ninaivu.server import settings_groups
from ninaivu.server.config import ENV_ALWAYS_WINS, ENVIRONMENT, RUNTIME_ONLY, Config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    import os
    for name in list(os.environ):
        if name.startswith("NINAIVU_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NINAIVU_STATE_DIR", str(tmp_path))
    return tmp_path


def test_every_variable_config_reads_is_declared():
    source = (ROOT / "ninaivu" / "server" / "config.py").read_text(encoding="utf-8")
    named = set(re.findall(r'"(NINAIVU_[A-Z_]+)"', source))
    by_hand = {"NINAIVU_ROOTS", "NINAIVU_ROOT", "NINAIVU_STATE_DIR",
               # Where the portable build is, not a setting (ensure_dirs).
               "NINAIVU_PORTABLE", "NINAIVU_PROGRAM_DIR", "NINAIVU_HOME"}
    assert named - by_hand == set(ENVIRONMENT)


def test_each_variable_names_one_setting():
    declared = [f.metadata.get("env") for f in fields(Config) if f.metadata.get("env")]
    assert len(declared) == len(set(declared))


def test_the_two_sets_come_from_the_declarations():
    assert RUNTIME_ONLY == {"state_dir", "host", "port", "admin_host", "admin_port", "debug",
                            "server_threads", "trusted_proxies", "max_upload_mb", "index_file",
                            "replica_from", "replica_primary_url", "replica_wake_mac"}
    assert ENV_ALWAYS_WINS == {"roots", "active_root", "host", "port", "admin_port",
                               "admin_host", "max_upload_mb", "debug", "server_threads",
                               "trusted_proxies", "backup_dir", "ai_models_dir"}


@pytest.mark.parametrize("var, raw, name, expected", [
    ("NINAIVU_PORT", "0", "port", 1),                         # held to 1..65535
    ("NINAIVU_PORT", "70000", "port", 65535),
    ("NINAIVU_PORT", "eighty", "port", 80),                   # not a number: the default
    ("NINAIVU_ASK", "0", "ask_ninaivu", False),               # its own name, not the field's
    ("NINAIVU_FACES", "yes", "faces_enabled", True),
    ("NINAIVU_CLOUD_FOLDER", "Family", "cloud_folder_name", "Family"),
    ("NINAIVU_LOCK_AFTER_MINUTES", "-3", "lock_after_minutes", 0),
    ("NINAIVU_WORKERS", "999", "workers", 64),
    ("NINAIVU_ADMIN_HOST", "", "admin_host", ""),             # text as given, empty too
])
def test_a_variable_is_read_as_its_declaration_says(clean_env, monkeypatch, var, raw, name, expected):
    monkeypatch.setenv(var, raw)
    assert getattr(Config.load(), name) == expected


def test_a_behaviour_variable_only_fills_in_what_nobody_chose(clean_env, monkeypatch):
    (clean_env / "config.json").write_text(json.dumps({"ask_ninaivu": True, "port": 8080}))
    monkeypatch.setenv("NINAIVU_ASK", "0")          # the console's choice stands
    monkeypatch.setenv("NINAIVU_PORT", "9090")      # where things are: the variable wins
    cfg = Config.load()
    assert cfg.ask_ninaivu is True
    assert cfg.port == 9090


def test_every_setting_is_in_one_layer():
    for f in fields(Config):
        assert settings_groups.layer_of(f.name) in settings_groups.LAYERS
    assert {n for n in (f.name for f in fields(Config))
            if settings_groups.layer_of(n) == "runtime"} == RUNTIME_ONLY
    assert settings_groups.layer_of("house_name") == "household"
    assert settings_groups.layer_of("thumb_quality") == "expert"


def test_the_reference_page_is_what_the_declarations_make():
    page = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8")
    assert page == settings_groups.reference() + "\n", \
        "docs/settings.md is out of date: run python tools/settings_doc.py"
