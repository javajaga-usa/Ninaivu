from pathlib import Path


def test_photo_studio_css_supports_light_and_dark_themes():
    editor_css = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'css' / 'editor.css').read_text(encoding='utf-8')
    assert ':root' in editor_css
    assert ':root[data-theme="dark"]' in editor_css
    assert ':root:not([data-theme="light"])' in editor_css
    assert '--pe-bg:' in editor_css
    assert '--pe-header-bg:' in editor_css
    assert '--pe-workspace-bg:' in editor_css
    assert '.pe-theme-toggle' in editor_css
    assert '.ico-sun' in editor_css
    assert '.ico-moon' in editor_css


def test_ai_playground_css_supports_light_and_dark_themes():
    playground_css = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'css' / 'ai-playground.css').read_text(encoding='utf-8')
    assert ':root' in playground_css
    assert ':root[data-theme="dark"]' in playground_css
    assert ':root:not([data-theme="light"])' in playground_css
    assert '--ap-bg:' in playground_css
    assert '--ap-header-bg:' in playground_css
    assert '--ap-workspace-bg:' in playground_css
    assert '.ap-theme-btn' in playground_css
    assert '.ico-sun' in playground_css
    assert '.ico-moon' in playground_css


def test_photo_editor_js_has_theme_toggle_and_shortcuts():
    editor_js = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'js' / 'editor.js').read_text(encoding='utf-8')
    assert 'pe-theme-btn' in editor_js
    assert 'cycleTheme' in editor_js
    assert "e.key.toLowerCase() === 't'" in editor_js


def test_ai_playground_js_has_theme_toggle_and_shortcuts():
    playground_js = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'js' / 'ai-playground' / 'components' / 'playground.js').read_text(encoding='utf-8')
    assert 'ap-theme-btn' in playground_js
    assert 'cycleAppTheme' in playground_js
    assert "e.key.toLowerCase() === 't'" in playground_js


def test_app_and_admin_sync_theme_storage():
    app_js = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
    admin_js = (Path(__file__).parents[1] / 'ninaivu' / 'static' / 'js' / 'admin.js').read_text(encoding='utf-8')
    assert 'window.cycleTheme = cycleTheme;' in app_js
    assert 'window.applyTheme = applyTheme;' in app_js
    assert 'ninaivu.theme' in app_js
    assert 'mv.theme' in admin_js
    assert "event.key === 'mv.theme'" in app_js
    assert "event.key === 'ninaivu.theme'" in admin_js


def test_ai_playground_markup_never_hard_codes_a_theme_colour():
    """Inline styles in the Playground must use the --ap-* tokens, not raw hex.

    The Playground builds much of its markup in JavaScript, and a literal colour
    written into one of those style attributes only suits the theme it was
    sampled from. Six controls in the generative panel carried the dark card
    colour, so in light mode they drew near-black text on a near-black field and
    the dropdowns could not be read at all. The CSS is theme-aware; the markup
    has to be too.

    Colours that are the content rather than the chrome -- the hair-tone
    swatches and the colour picker beside them -- are exempt: those are the
    values being chosen, and they mean the same thing in either theme.
    """
    import re
    root = Path(__file__).parents[1] / 'ninaivu' / 'static' / 'js' / 'ai-playground'
    offenders = []
    for path in sorted(root.rglob('*.js')):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if 'ap-swatch' in line or 'type="color"' in line:
                continue
            for hit in re.findall(r'(?:background|color):\s*#[0-9a-fA-F]{3,8}', line):
                offenders.append(f'{path.name}:{number}: {hit}')
    assert not offenders, 'use a --ap-* variable instead:\n' + '\n'.join(offenders)
