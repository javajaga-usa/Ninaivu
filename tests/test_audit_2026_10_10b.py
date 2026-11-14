"""The audit of 10 October 2026, second pass: sign-in pauses and resumed
compressions. (The phone inbox's tests are in test_phone_inbox.py, the TV
album's in test_tv_album.py, Keepsake's in test_audit_2026_10_10b_keepsake.py.)"""

from __future__ import annotations

import time

from ninaivu.api import accounts_api


def test_a_flood_of_made_up_names_does_not_displace_a_real_profiles_pause(monkeypatch):
    now = time.time()
    monkeypatch.setattr(accounts_api, "_LOCKOUTS", {})
    monkeypatch.setattr(accounts_api, "_ATTEMPTS", {})
    monkeypatch.setattr(accounts_api, "_MAX_KEYS", 100)
    monkeypatch.setattr(accounts_api, "_last_sweep", 0.0)
    # A real profile, paused three times; its pause ends before the flood's.
    accounts_api._LOCKOUTS["*|user:dad"] = (3, now + 60)
    for i in range(100):
        accounts_api._LOCKOUTS[f"*|user:made-up-{i}"] = (1, now + 1800)
    accounts_api._sweep(now)
    assert accounts_api._LOCKOUTS.get("*|user:dad") == (3, now + 60)


