import re
from pathlib import Path


def test_index_html_has_offline_elements():
    index_html = (Path(__file__).parents[1] / 'ninaivu' / 'templates' / 'index.html').read_text(encoding='utf-8')
    assert 'id="offline-banner"' in index_html
    assert 'id="offline-retry-btn"' in index_html
    assert 'empty-art-offline' in index_html
    assert 'empty-art-default' in index_html


def test_style_css_has_offline_rules():
    style_css = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'css' / 'style.css').read_text(encoding='utf-8')
    assert '.offline-banner' in style_css
    assert '.offline-banner-dot' in style_css
    assert '.ai-badge.offline' in style_css
    assert '.empty.offline' in style_css
    assert 'pulse-offline' in style_css


def test_app_js_handles_offline_and_reconnection():
    app_js = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
    assert 'setServerOffline' in app_js
    assert 'checkConnection' in app_js
    assert 'isNetworkFailure' in app_js
    assert 'finishStartup' in app_js
    assert 'offline-retry-btn' in app_js
    assert 'reconnectTimer' in app_js


def test_service_worker_cache_version_bumped():
    """At least the version that dropped caches built before offline handling.

    Pinning one exact string failed every later bump — which is the thing this
    test exists to encourage — so it checks the number has not gone backwards.
    """
    sw_js = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'sw.js').read_text(encoding='utf-8')
    match = re.search(r"CACHE_VERSION = 'v(\d+)-[\w-]+'", sw_js)
    assert match, 'CACHE_VERSION is missing or no longer vN-name'
    assert int(match.group(1)) >= 8
