"""Keep the panel responsive and avoid background I/O nobody can see."""
import queue
import threading
from types import SimpleNamespace

import pytest

from ninaivu.desktop import app


@pytest.mark.parametrize('visible,paused,expected_reads', [
    (False, False, 0), (True, True, 0), (True, False, 1),
])
def test_logs_read_only_when_visible_and_unpaused(visible, paused, expected_reads):
    finished = threading.Event()
    reads = []
    tail = SimpleNamespace(read=lambda: reads.append(True) or 'new log',
                           path=SimpleNamespace(exists=lambda: True))
    dashboard = SimpleNamespace(finished=finished, log_tail=tail, log_generation=1,
                                logs_visible=visible, logs_paused=paused, events=queue.Queue(),
                                log_wake=SimpleNamespace(wait=lambda _: finished.set(), clear=lambda: None))
    app.Dashboard.log_loop(dashboard)
    assert len(reads) == expected_reads
    assert dashboard.events.qsize() == expected_reads


def test_event_flood_yields_to_the_ui_without_losing_order():
    events = queue.Queue()
    seen = []
    for i in range(250):
        events.put(('call', lambda i=i: seen.append(i)))
    dashboard = SimpleNamespace(events=events)
    app.Dashboard._pump_events(dashboard)
    assert seen == list(range(100))
    assert events.qsize() == 150
    app.Dashboard._pump_events(dashboard)
    app.Dashboard._pump_events(dashboard)
    assert seen == list(range(250))


def test_power_worker_recovers_after_temporary_sensor_failure(monkeypatch):
    class Finished:
        attempts = 0
        def is_set(self): return self.attempts >= 2
        def wait(self, seconds): self.attempts += 1
    answers = iter([RuntimeError('sensor disappeared'), 12.5])
    def reading():
        value = next(answers)
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(app, 'read_power', reading)
    dashboard = SimpleNamespace(finished=Finished(), events=queue.Queue())
    app.Dashboard.power_loop(dashboard)
    assert dashboard.events.get_nowait() == ('power', None)
    assert dashboard.events.get_nowait() == ('power', 12.5)
