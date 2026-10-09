def test_playground_navigation_and_modules_use_existing_app_shell(as_family):
    response = as_family.get('/')
    assert response.status_code == 200
    assert b'id="v-ai-playground"' in response.data
    assert b'id="nav-ai-playground"' not in response.data
    for path in ['components/playground.js', 'components/portrait.js', 'services/worker.mjs', 'services/AIPhotoService.mjs']:
        response = as_family.get('/static/js/ai-playground/' + path)
        assert response.status_code == 200
        assert 'javascript' in response.content_type


def test_release_version():
    import ninaivu
    import re
    from pathlib import Path
    metadata = (Path(__file__).parents[1] / 'pyproject.toml').read_text()
    assert re.fullmatch(r'\d+\.\d+\.\d+', ninaivu.__version__)
    root = Path(__file__).parents[1]
    import tomllib
    for extension in (root / 'extensions').glob('*/pyproject.toml'):
        extension_metadata = tomllib.loads(extension.read_text(encoding='utf-8'))
        assert extension_metadata['project']['version'] == ninaivu.__version__
    changelog = (root / 'docs' / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert any(line.startswith(f'## {ninaivu.__version__} — ') for line in changelog.splitlines())
    # One place for the version: pyproject reads it from the package.
    assert re.search(r'^version = \{attr = "ninaivu.__version__"\}$', metadata, re.M)
    assert not re.search(r'^version = "', metadata, re.M)
