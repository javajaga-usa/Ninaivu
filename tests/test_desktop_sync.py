"""Tests for bidirectional synchronization between terminal launchers and the desktop Control Center."""
from types import SimpleNamespace
import pytest


from ninaivu.desktop.control import Controller
from ninaivu.desktop.app import Dashboard


def test_capture_running_settings_infers_mode_from_workers(tmp_path, monkeypatch):
    c = Controller(root=tmp_path, cfg=SimpleNamespace(state_dir=tmp_path / 'state', host='127.0.0.1', port=5000, admin_port=3000, ai_engine='off'))
    monkeypatch.setattr(c, 'record', lambda: {'pid': 9999, 'port': 5000, 'admin_port': 3000, 'scheme': 'https'})

    class FakeProcess:
        def __init__(self, workers):
            self._workers = workers
        def cmdline(self):
            return ['python.exe', '-m', 'ninaivu', '--workers', str(self._workers), '--https']

    # Test power-saving inference
    from ninaivu.desktop import control as control_module
    fake_psutil = SimpleNamespace(Process=lambda pid: FakeProcess(1), Error=Exception)
    monkeypatch.setattr(control_module, 'psutil', fake_psutil)
    c.capture_running_settings()
    assert c.mode == 'power-saving'
    assert c.settings.get('scheme') == 'https'

    # Test performance inference
    fake_psutil.Process = lambda pid: FakeProcess(8)
    c.capture_running_settings()
    assert c.mode == 'performance'

    # Test standard inference
    fake_psutil.Process = lambda pid: FakeProcess(4)
    c.capture_running_settings()
    assert c.mode == 'standard'


def test_switch_log_local_ai_fallback(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError:
        pytest.skip("Tkinter display not available")

    try:
        runtime = tmp_path / '.ninaivu-control'
        runtime.mkdir(parents=True)
        ai_dir = tmp_path / '.ai-models'
        ai_dir.mkdir(parents=True)
        fallback_log = ai_dir / 'ollama-stdout.log'
        fallback_log.write_text("Local AI model loaded\n", encoding="utf-8")

        controller = SimpleNamespace(
            root=tmp_path,
            runtime=runtime,
            mode='standard',
            server_url=lambda *_: 'https://127.0.0.1',
            record=lambda: None,
            settings={},
            cfg=SimpleNamespace(state_dir=tmp_path / 'state'),
        )
        dashboard = Dashboard(root, controller)
        dashboard.log_source.set('Local AI')
        dashboard.switch_log()
        assert dashboard.log_tail.path == fallback_log
        assert "Local AI model loaded" in dashboard.log_text.get('1.0', 'end')
        dashboard.finished.set()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def test_display_terminal_launch_sync(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError:
        pytest.skip("Tkinter display not available")

    try:
        runtime = tmp_path / '.ninaivu-control'
        runtime.mkdir(parents=True)
        controller = SimpleNamespace(
            root=tmp_path,
            runtime=runtime,
            mode='standard',
            server_url=lambda *_: 'https://127.0.0.1',
            record=lambda: {'pid': 1234, 'port': 443, 'admin_port': 3000, 'scheme': 'https'},
            settings={'arguments': ['--workers', '8']},
            cfg=SimpleNamespace(state_dir=tmp_path / 'state'),
            capture_running_settings=lambda: None,
        )
        dashboard = Dashboard(root, controller)
        assert dashboard.status.get() == 'Checking server…'

        sample = {
            'running': True, 'cpu': 15.0, 'ram_percent': 42.0,
            'ram_used': 4 * 1024**3, 'ram_total': 16 * 1024**3,
            'server_cpu': 2.5, 'server_ram': 128 * 1024**2,
            'threads': 8, 'battery': 85.0, 'plugged': True,
            'disk_free': 50 * 1024**3, 'uptime': 120,
        }
        dashboard.display(sample)
        assert dashboard.status.get() == '● Running'
        assert dashboard.start_button.instate(['disabled'])
        assert not dashboard.stop_button.instate(['disabled'])

        # Now simulate terminal stop
        sample_stopped = dict(sample, running=False)
        controller.record = lambda: None
        dashboard.display(sample_stopped)
        assert dashboard.status.get() == '○ Stopped'
        assert not dashboard.start_button.instate(['disabled'])
        assert dashboard.stop_button.instate(['disabled'])
        dashboard.finished.set()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

