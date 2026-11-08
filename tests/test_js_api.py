"""static/js/api.js: one function per route, made from the route manifest."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "ninaivu" / "static" / "js"


def _tool():
    spec = importlib.util.spec_from_file_location("js_api", ROOT / "tools" / "js_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_checked_in_api_js_is_current():
    assert (JS / "api.js").read_text(encoding="utf-8") == _tool().build(), \
        "ninaivu/static/js/api.js is out of date: run python tools/js_api.py"


def test_every_api_route_has_its_function_on_its_own_face():
    tool = _tool()
    text = (JS / "api.js").read_text(encoding="utf-8")
    home, admin = text.split("export const admin")
    routes = tool.parse(tool.MANIFEST.read_text(encoding="utf-8"))
    assert len(routes) > 500
    for face, method, rule in routes:
        name, line = tool.function(method, rule)
        assert line.strip() in (home if face == "home" else admin), (face, method, rule)


def test_a_console_only_route_is_not_offered_to_the_family_app():
    text = (JS / "api.js").read_text(encoding="utf-8")
    home = text.split("export const admin")[0]
    assert "getAdminOverview" not in home
    assert "getAdminOverview:" in text.split("export const admin")[1]


def test_names_and_urls():
    tool = _tool()
    assert tool.function("GET", "/api/admin/overview") == (
        "getAdminOverview",
        "  getAdminOverview: (options) => send('GET', '/api/admin/overview', options),")
    name, line = tool.function("POST", "/api/assets/<int:asset_id>/rotate")
    assert name == "postAssetsByAssetIdRotate"
    assert "(assetId, options)" in line and "`/api/assets/${seg(assetId)}/rotate`" in line
    name, line = tool.function("GET", "/api/files/<path:folder>")
    assert "${path(folder)}" in line


def test_the_hand_written_layer_is_still_found_through_api_js():
    """Every module that imports `api`, `thumbUrl`, … from './api.js' keeps working."""
    text = (JS / "api.js").read_text(encoding="utf-8")
    assert "export * from './http.js';" in text
    http = (JS / "http.js").read_text(encoding="utf-8")
    for name in ("export const api", "export function thumbUrl", "export function consoleJson",
                 "export function homeSend", "export function withQuery"):
        assert name in http.replace("export async function", "export function"), name


@pytest.mark.skipif(not shutil.which("node"), reason="needs Node")
def test_the_functions_build_the_requests_the_hand_written_ones_did(tmp_path):
    """Run api.js under Node with a fetch that records what it was asked."""
    script = tmp_path / "check.mjs"
    script.write_text(f"""
const asked = [];
globalThis.fetch = async (url, options = {{}}) => {{
  asked.push([url, options.method || 'GET', options.body || null,
              (options.headers || {{}})['X-Requested-With'] || null]);
  return {{ ok: true, status: 200, statusText: 'OK', text: async () => '{{"ok":true}}' }};
}};
globalThis.localStorage = {{ getItem: () => null, setItem() {{}} }};
globalThis.document = {{ documentElement: {{ lang: 'en' }}, addEventListener() {{}} }};
Object.defineProperty(globalThis, 'navigator', {{ value: {{ languages: ['en'], language: 'en' }}, configurable: true }});
const {{ home, admin }} = await import({str(JS / 'api.js')!r});
await admin.getAdminOverview();
await admin.postAdminBackupsNow();
await admin.postVisibilityFolder({{ body: {{ folder: 'a b', visibility: 'hidden', confirm: false }} }});
await admin.getAdminLocations({{ query: {{ hours: 3 }} }});
await home.getAlbumsByAlbumId(7);
await home.postAssetByAssetId(9, {{ body: {{ favorite: true }} }});
console.log(JSON.stringify(asked));
""", encoding="utf-8")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    import json
    asked = json.loads(done.stdout.strip().splitlines()[-1])
    assert asked == [
        ["/api/admin/overview", "GET", None, None],
        ["/api/admin/backups/now", "POST", None, None],
        ["/api/visibility/folder", "POST",
         '{"folder":"a b","visibility":"hidden","confirm":false}', None],
        ["/api/admin/locations?hours=3", "GET", None, None],
        ["/api/albums/7", "GET", None, "fetch"],
        ["/api/asset/9", "POST", '{"favorite":true}', "fetch"],
    ]
