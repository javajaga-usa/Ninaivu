"""Linux Control Panel actions against a real, isolated Ninaivu server."""
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

from ninaivu.desktop.control import Controller
from ninaivu.server.config import Config

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux app lifecycle')


def _port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_start_stop_restart_mode_urls_and_logs_use_one_home_index(tmp_path, monkeypatch):
    project = tmp_path / "Ninaivu's app"
    project.mkdir()
    (project / 'ninaivu').symlink_to(Path(__file__).resolve().parents[1] / 'ninaivu',
                                    target_is_directory=True)
    home = tmp_path / 'home'
    state = home / '.ninaivu'
    photos = tmp_path / 'photos'
    photos.mkdir()
    for key in ('NINAIVU_PORTABLE', 'NINAIVU_PORTABLE_ROOT', 'NINAIVU_PROGRAM_DIR'):
        monkeypatch.delenv(key, raising=False)
    for key, value in {'HOME': home, 'NINAIVU_HOME': project, 'NINAIVU_STATE_DIR': state,
                       'NINAIVU_PYTHON': sys.executable, 'NINAIVU_AI_MODELS_DIR': tmp_path / 'models',
                       'NINAIVU_LOG_DIR': project / 'logs'}.items():
        monkeypatch.setenv(key, str(value))
    port, admin = _port(), _port()
    while admin == port:
        admin = _port()
    cfg = Config(state_dir=state, host='127.0.0.1', port=port, admin_port=admin, ai_engine='off')
    cfg.roots = [str(photos)]
    cfg.save()
    controller = Controller(root=project, cfg=cfg)
    controller.settings['arguments'] = ['--host', '127.0.0.1', '--port', str(port),
        '--admin-port', str(admin), '--ai', 'off', '--no-watch', '--no-mdns']
    try:
        assert controller.start() == 'Ninaivu is running.'
        assert controller.cfg.db_path == state / 'index.db'
        with urllib.request.urlopen(controller.server_url(), timeout=10) as response:
            assert response.status == 200
        first = controller.record()['pid']
        assert 'stopped' in controller.stop().lower()
        assert controller.record() is None
        assert controller.start() == 'Ninaivu is running.'
        assert controller.record()['pid'] != first
        controller.apply_mode('power-saving')
        assert controller.mode == 'power-saving'
        assert controller.record() is not None
        assert (controller.logs / 'server.log').is_file()
        assert controller.cfg.db_path == state / 'index.db'
    finally:
        try:
            controller.stop()
        finally:
            if controller.started is not None and controller.started.poll() is None:
                controller.started.terminate()
                controller.started.wait(timeout=15)


def test_unavailable_index_is_shown_in_a_dialog_instead_of_silent_exit(monkeypatch):
    from types import SimpleNamespace
    from ninaivu.desktop import app
    events = []
    root = SimpleNamespace(iconphoto=lambda *_: None, withdraw=lambda: events.append('hide'),
        deiconify=lambda: None, destroy=lambda: events.append('destroy'))
    monkeypatch.setattr(app, 'tk', SimpleNamespace(Tk=lambda **_: root,
        PhotoImage=lambda **_: object(), TclError=RuntimeError))
    monkeypatch.setattr(app, 'missing_requirement', lambda: None)
    monkeypatch.setattr(app, 'offer_the_older_library', lambda _: None)
    def unavailable(_):
        raise ValueError('The relocated index is unavailable. Reconnect its drive.')
    monkeypatch.setattr(app, 'Dashboard', unavailable)
    monkeypatch.setattr(app.messagebox, 'showerror', lambda _title, text, **_: events.append(text))
    assert app.main() == 1
    assert any('unavailable' in event for event in events)
    assert events[-1] == 'destroy'
