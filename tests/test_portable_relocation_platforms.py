"""Portable handover/rollback for each platform's on-disk layout, without OS APIs."""
import json
import sqlite3
from contextlib import closing

import pytest

from ninaivu.desktop import portable
from ninaivu.storage import relocate, upgrade


def _package(path, system, label=None):
    path.mkdir()
    if system == 'darwin':
        (path / '.ninaivu-portable').touch()
        (path / 'Ninaivu.app').mkdir()
    else:
        (path / ('Ninaivu.exe' if system == 'win32' else 'ninaivu')).touch()
        (path / 'app/python').mkdir(parents=True)
    state = path / ('.ninaivu' if system == 'linux' else 'data')
    if label:
        state.mkdir()
        with closing(sqlite3.connect(state / 'index.db')) as conn, conn:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('CREATE TABLE assets (id INTEGER PRIMARY KEY, words TEXT)')
            conn.execute('INSERT INTO assets(words) VALUES (?)', (label,))
        (state / 'config.json').write_text('{}')
        (state / upgrade.VERSION_FILE).write_text(json.dumps({'version': '1.0.0'}))
    return state


@pytest.mark.parametrize('system', ['linux', 'darwin', 'win32'])
@pytest.mark.parametrize('when', ['before-upgrade', 'after-upgrade'])
def test_each_portable_layout_rolls_back_the_live_relocated_index(tmp_path, monkeypatch, system, when):
    monkeypatch.setattr(portable, '_system', lambda platform=None: system)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    older, newer = tmp_path / 'older app', tmp_path / 'newer app'
    original = _package(older, system, 'family before upgrade')
    _package(newer, system)
    external = tmp_path / 'chosen index'
    def move(state):
        relocate.request(state, external)
        assert relocate.apply_pending(state) == external
    if when == 'before-upgrade':
        move(original)
    portable.bring_here(portable._layout(older), portable._layout(newer), version='2.0.0',
                        stop_older=lambda _: None)
    live = portable._layout(newer).state
    assert upgrade.prepare(live, version='2.0.0', environ={})['backup']
    with closing(sqlite3.connect(live / 'index.db')) as conn, conn:
        conn.execute("UPDATE assets SET words='newer changes'")
    if when == 'after-upgrade':
        move(live)
    result = portable.go_back(portable._layout(newer), version='2.0.0', stop_this=lambda _: None)
    with closing(sqlite3.connect(external / 'index.db')) as conn:
        assert conn.execute('SELECT words FROM assets').fetchone()[0] == 'family before upgrade'
    assert result['kept']
    assert portable._layout(older).state == external
    assert portable._layout(newer).moved_to == older
