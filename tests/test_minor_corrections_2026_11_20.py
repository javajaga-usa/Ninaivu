"""Small corrections found by walking every console page and the family app in Tamil.

Three console fields carried ``data-i18n-aria-label``, a marker ``i18n.apply``
does not know, so they stayed English; a few labels had no marker at all; and
some sentences the server sends (archive health, the off-site copy, the face
models, the Performance page's drive advice) reached a Tamil page in English.
"""
from __future__ import annotations

import re
from pathlib import Path

from ninaivu.server import capacity
from ninaivu.words import Said

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ninaivu" / "static"
TEMPLATES = ROOT / "ninaivu" / "templates"
I18N = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")


def test_every_translation_marker_in_the_pages_is_one_i18n_knows():
    known = set(re.findall(r"\['(data-i18n-[a-z]+)', '[a-z-]+'\]", I18N))
    known |= {"data-i18n-rich", "data-i18n-vars"}
    assert {"data-i18n-title", "data-i18n-label", "data-i18n-placeholder"} <= known
    for page in [*TEMPLATES.glob("*.html"), *(STATIC / "js").rglob("*.js")]:
        used = set(re.findall(r"data-i18n-[a-z-]+(?==)", page.read_text(encoding="utf-8")))
        assert used <= known, f"{page.name}: {sorted(used - known)}"


def test_the_drive_in_the_performance_advice_is_said_whole():
    drive = {"role": "index", "present": True, "internal": False, "solid_state": False,
             "path": "/Volumes/x"}
    advice = next(a for a in capacity.recommend({"storage": [drive]}) if a["id"] == "index-drive")
    assert advice["detail"].startswith("The index and thumbnails are on an external spinning drive.")
    assert advice["detail_params"]["drive"] == {"said": "an external spinning drive", "params": {}}


def test_a_drive_on_a_named_bus_keeps_its_name_out_of_the_translation():
    said = capacity._connection({"internal": False, "bus": "USB", "solid_state": True})
    assert isinstance(said, Said)
    assert str(said) == "a USB solid-state drive"
    assert said.as_json() == {"said": "a {bus} solid-state drive", "params": {"bus": "USB"}}


def test_the_scan_advice_is_a_template_with_its_numbers():
    facts = {"storage": [{"path": "/lib", "present": True, "filesystem": "exfat",
                          "internal": False, "bus": "USB"}],
             "scans": {"/lib": {"seconds": 125, "files": 20000, "files_per_second": 160}}}
    advice = next(a for a in capacity.recommend(facts) if a["id"] == "scan:/lib")
    assert advice["detail"].startswith(
        "A scan that found nothing new took 2 min 5 s to look at 20,000 files (160 a second), "
        "on a USB drive formatted EXFAT.")
    assert "{drive}" in advice["detail_key"]


def test_the_console_translates_what_the_server_says_on_these_pages():
    js = {name: (STATIC / "js" / name).read_text(encoding="utf-8")
          for name in ("archive.js", "faces.js", "performance.js", "viewer.js")}
    assert "i18n.fromServer(health.message_said)" in js["archive.js"]
    assert "i18n.fromServer(engine.reason_said)" in js["faces.js"]
    assert "i18n.fromServer({ said: item.detail_key, params: item.detail_params })" in js["performance.js"]
    assert "[i18n.t('Location')," in js["viewer.js"]
    assert "i18n.t(KIND_WORDS[item.kind] || item.kind)" in js["viewer.js"]
