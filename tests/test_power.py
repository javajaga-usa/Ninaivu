"""Race-to-idle power policy: efficient serving, fast archive work."""

import sys

from ninaivu.utils import power
from ninaivu.utils.power import PowerPolicy


def windows_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        power, "_set_windows_execution_speed_throttled",
        lambda throttled: calls.append(throttled) or True,
    )
    return PowerPolicy(), calls


def test_windows_serving_uses_efficiency_mode(monkeypatch):
    policy, calls = windows_policy(monkeypatch)

    assert policy.start() is True
    assert calls == [True]
    assert policy.snapshot() == {
        "mode": "efficient", "managed": True, "archive_active": False,
    }


def test_archive_gets_full_speed_then_returns_to_efficiency(monkeypatch):
    policy, calls = windows_policy(monkeypatch)
    policy.start()

    policy.archive_started()
    assert policy.snapshot()["mode"] == "performance"
    assert policy.snapshot()["archive_active"] is True

    policy.archive_finished()
    assert calls == [True, False, True]
    assert policy.snapshot()["mode"] == "efficient"
    assert policy.snapshot()["archive_active"] is False


def test_nested_archive_holds_performance_until_last_finish(monkeypatch):
    policy, calls = windows_policy(monkeypatch)
    policy.start()
    policy.archive_started()
    policy.archive_started()

    policy.archive_finished()
    assert policy.snapshot()["mode"] == "performance"
    policy.archive_finished()

    assert calls == [True, False, True]
    assert policy.snapshot()["mode"] == "efficient"


def test_stop_removes_the_windows_hint(monkeypatch):
    policy, calls = windows_policy(monkeypatch)
    policy.start()
    policy.stop()

    assert calls == [True, False]
    assert policy.snapshot()["mode"] == "unmanaged"


def test_other_platforms_are_safe_no_ops(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    policy = PowerPolicy()

    assert policy.start() is False
    assert policy.archive_started() is False
    assert policy.archive_finished() is False
    assert policy.snapshot()["mode"] == "unmanaged"


def test_a_refused_windows_hint_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        power, "_set_windows_execution_speed_throttled", lambda _value: False,
    )
    policy = PowerPolicy()

    assert policy.start() is False
    assert policy.snapshot()["managed"] is False
    assert policy.snapshot()["mode"] == "unmanaged"
