"""A start is waited on while the server says how far it has got.

The Control Panel gave up on a start after a fixed minute. The first start
after an update copies the whole index first (a minute for 2.7 GB), so a
healthy server on a big library got "Startup is taking longer than
expected" on every update.
"""
import json
import time
from types import SimpleNamespace

import pytest

from ninaivu.desktop import control as module
from ninaivu.desktop.control import Controller
from ninaivu.server import starting


class Clock:
    """Time that moves only when the wait sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(module, '_server_process', lambda record: None)
    c = Controller(root=tmp_path, cfg=SimpleNamespace(
        state_dir=tmp_path / 'state', host='127.0.0.1', port=5000, admin_port=3000,
        ai_engine='off'))
    c.started = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(c, 'capture_running_settings', lambda: None)
    return c


def note(state, step, since, at):
    state.mkdir(parents=True, exist_ok=True)
    (state / starting.STARTING_FILE).write_text(json.dumps(
        {'pid': 1, 'step': step, 'since': since, 'at': at}), encoding='utf-8')


def test_a_start_that_keeps_saying_how_it_goes_is_waited_on_past_a_minute(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    clock = Clock()
    launched = time.time()
    done_at = 5 * 60           # five minutes copying the index
    monkeypatch.setattr(c, 'record', lambda: {'pid': 1} if clock.now >= done_at else None)

    def read(state_dir, now=None):
        return {'step': 'copying the settings and the index before the update',
                'since': launched, 'at': time.time()}
    monkeypatch.setattr(starting, 'read', read)
    heard = []
    c.on_progress = lambda what, seconds: heard.append((what, seconds))
    assert c._wait_for_start(launched, clock=clock, sleep=clock.sleep) == 'Ninaivu is running.'
    assert heard[-1][0] == 'copying the settings and the index before the update'
    assert heard[-1][1] > 60


def test_a_start_that_goes_quiet_is_reported_with_its_step(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    clock = Clock()
    launched = time.time()
    monkeypatch.setattr(c, 'record', lambda: None)
    monkeypatch.setattr(starting, 'read', lambda state_dir, now=None: (
        {'step': 'opening the index', 'since': launched, 'at': time.time()}
        if clock.now < 20 else None))
    with pytest.raises(RuntimeError, match='stopped saying.*opening the index'):
        c._wait_for_start(launched, clock=clock, sleep=clock.sleep)
    assert clock.now == pytest.approx(20 + module.QUIET_SECONDS, abs=1)


def test_a_start_that_never_says_anything_is_given_its_first_minutes(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    clock = Clock()
    monkeypatch.setattr(c, 'record', lambda: {'pid': 1} if clock.now >= 120 else None)
    monkeypatch.setattr(starting, 'read', lambda state_dir, now=None: None)
    assert c._wait_for_start(time.time(), clock=clock, sleep=clock.sleep) == 'Ninaivu is running.'

    clock.now = 0
    monkeypatch.setattr(c, 'record', lambda: None)
    with pytest.raises(RuntimeError, match='has not said'):
        c._wait_for_start(time.time(), clock=clock, sleep=clock.sleep)
    assert clock.now == pytest.approx(module.FIRST_WORD_SECONDS, abs=1)


def test_a_note_left_by_an_earlier_start_does_not_count(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    clock = Clock()
    launched = time.time()
    monkeypatch.setattr(c, 'record', lambda: None)
    monkeypatch.setattr(starting, 'read', lambda state_dir, now=None: (
        {'step': 'opening the index', 'since': launched - 3600, 'at': time.time()}))
    with pytest.raises(RuntimeError, match='has not said'):
        c._wait_for_start(launched, clock=clock, sleep=clock.sleep)


def test_a_start_that_ends_is_still_reported_as_failed(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c.started = SimpleNamespace(poll=lambda: 1)
    monkeypatch.setattr(c, 'record', lambda: None)
    monkeypatch.setattr(c, 'failed_start', lambda: None)
    with pytest.raises(RuntimeError, match='could not start'):
        c._wait_for_start(time.time(), sleep=lambda _: None)


def test_the_server_writes_its_step_and_removes_the_note_when_done(tmp_path):
    state = tmp_path / 'state'
    starting.begin(state)
    try:
        assert starting.read(state)['step'] == 'starting'
        starting.step('copying the settings and the index before the update')
        seen = starting.read(state)
        assert seen['step'] == 'copying the settings and the index before the update'
        assert seen['since'] <= seen['at']
    finally:
        starting.end()
    assert not (state / starting.STARTING_FILE).exists()
    assert starting.read(state) is None


def test_a_stale_note_is_not_read(tmp_path):
    state = tmp_path / 'state'
    note(state, 'opening the index', time.time() - 100, time.time() - starting.FRESH_SECONDS - 1)
    assert starting.read(state) is None
    note(state, 'opening the index', time.time() - 100, time.time())
    assert starting.read(state)['step'] == 'opening the index'


def test_the_copy_before_an_update_is_named_as_a_step(tmp_path, monkeypatch):
    from ninaivu.storage import backup, upgrade
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'index.db').write_bytes(b'')
    steps = []
    monkeypatch.setattr(starting, 'step', steps.append)
    monkeypatch.setattr(upgrade, 'schemas', lambda _: {})
    made = tmp_path / 'copy.tar.gz'
    monkeypatch.setattr(backup, 'snapshot', lambda *a, **k: made)
    monkeypatch.setattr(backup, 'prune', lambda *a, **k: None)
    upgrade.prepare(state, version='9.9.9', environ={})
    assert steps == ['copying the settings and the index before the update']


def test_describe_wait():
    assert module.describe_wait(45) == '45 seconds'
    assert module.describe_wait(300) == '5 minutes'
