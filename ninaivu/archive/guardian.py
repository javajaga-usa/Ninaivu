"""Scheduled, read-only sampling of archive integrity and free space."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from typing import Callable

from . import database as db
from .safety import long_path
from .scanner import hash_file


class ArchiveGuardian:
    """Continuously maintain a cheap health signal for a large archive.

    A full audit is intentionally explicit because it can reread terabytes.
    Guardian instead rotates through a bounded sample once per day, eventually
    covering every verified file, and checks destination free space. It writes
    only its small result record in ``archive.db``; archive media is read-only.
    """

    def __init__(self, is_archive_running: Callable[[], bool], *,
                 interval: float = 24 * 60 * 60, sample_size: int = 64) -> None:
        self.is_archive_running = is_archive_running
        self.interval = interval
        self.sample_size = sample_size
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._check_thread: threading.Thread | None = None
        self._running = False
        self._state = db.load_guardian_state()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop,
                                            name='archive-guardian', daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None

    def _loop(self) -> None:
        # Check shortly after boot, then hourly for whether the daily run is due.
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            last = float(self._state.get('checked_at') or 0)
            # Soon for what may put itself right within minutes: a check that
            # could not finish, an unplugged drive, and a nearly full disk
            # (for its space only: sample_due keeps the read-back daily).
            retry = 300 if (self._state.get('retry_soon')
                            or self._state.get('status') in ('no-archive', 'warning')) else self.interval
            if time.time() - last >= retry and not self.is_archive_running():
                self.run_once(sample=self.sample_due())
            self._stop.wait(min(300, max(5, retry)))

    def sample_due(self) -> bool:
        """Whether the next scheduled check reads files back, or only looks at
        the free space.

        A disk nearly full ("warning") is looked at again every five minutes,
        for the space. Reading the sample back each time as well kept a nearly
        full archive disk busy all day; the sample keeps to its own interval.
        """
        if self._state.get('status') != 'warning':
            return True
        return time.time() - float(self._state.get('sampled_at') or 0) >= self.interval

    def request_check(self) -> bool:
        with self._lock:
            if self._running or self.is_archive_running():
                return False
            self._check_thread = threading.Thread(
                target=self.run_once, name='archive-health-check', daemon=True)
            self._check_thread.start()
            return True

    @staticmethod
    def _fingerprint(state: dict) -> str:
        meaningful = {
            'status': state.get('status'),
            'problems': sorted((p.get('path'), p.get('kind'))
                               for p in state.get('problems', [])),
            # Notify as free space crosses broad boundaries, not for every byte.
            'free_band': int((state.get('free_percent') or 0) // 5),
        }
        return hashlib.sha256(json.dumps(meaningful, sort_keys=True).encode()).hexdigest()

    def run_once(self, sample: bool = True) -> dict:
        with self._lock:
            if self._running:
                state = dict(self._state)
                state['running'] = True
                return state
            self._running = True
        try:
            settings = db.load_settings()
            destination = settings.get('destination_dir') or ''
            previous = dict(self._state)
            state = {
                'status': 'no-archive', 'checked_at': time.time(),
                'checked': 0, 'matched': 0, 'missing': 0, 'mismatched': 0,
                'unreadable': 0, 'problems': [], 'destination': destination,
                'cursor': int(previous.get('cursor') or 0),
                'change_id': int(previous.get('change_id') or 0),
            }
            if not destination or not os.path.isdir(long_path(destination)):
                state['status'] = 'critical' if destination else 'no-archive'
                state['retry_soon'] = bool(destination)
                state['message'] = ('Archive destination is unavailable.' if destination
                                    else 'No archive has been created yet.')
            else:
                usage = shutil.disk_usage(long_path(destination))
                state.update(free_bytes=usage.free, total_bytes=usage.total,
                             free_percent=round(usage.free * 100 / usage.total, 1)
                             if usage.total else 0)
                if sample:
                    rows = db.guardian_candidates(state['cursor'], self.sample_size)
                    state['sampled_at'] = time.time()
                else:
                    # The last sample's findings stand; only the space is new.
                    rows = []
                    for key in ('checked', 'matched', 'missing', 'mismatched', 'unreadable'):
                        state[key] = int(previous.get(key) or 0)
                    state['problems'] = list(previous.get('problems') or [])
                    state['sampled_at'] = float(previous.get('sampled_at') or 0)
                for row in rows:
                    state['checked'] += 1
                    state['cursor'] = row['id']
                    path = row['destination_path']
                    if not os.path.isfile(long_path(path)):
                        state['missing'] += 1
                        state['problems'].append({'kind': 'missing', 'path': path})
                        continue
                    try:
                        actual = hash_file(path)
                    except OSError as exc:
                        state['unreadable'] += 1
                        state['problems'].append({
                            'kind': 'unreadable', 'path': path, 'error': str(exc)})
                        continue
                    if actual != row['file_hash']:
                        state['mismatched'] += 1
                        state['problems'].append({'kind': 'changed', 'path': path})
                    else:
                        state['matched'] += 1
                bad = state['missing'] + state['mismatched'] + state['unreadable']
                if bad:
                    state['status'] = 'critical'
                    state['message'] = f'{bad} sampled archive file(s) need attention.'
                elif state['free_percent'] < 10:
                    state['status'] = 'warning'
                    state['message'] = 'Archive storage has less than 10% free space.'
                elif not (rows if sample else state['checked']):
                    state['status'] = 'no-archive'
                    state['message'] = 'No verified archive files to sample yet.'
                else:
                    state['status'] = 'healthy'
                    state['message'] = f'{state["matched"]} sampled files matched.'

            state['problems'] = state['problems'][:20]
            fingerprint = self._fingerprint(state)
            if fingerprint != previous.get('fingerprint'):
                state['change_id'] += 1
            state['fingerprint'] = fingerprint
            with self._lock:
                self._state = state
            db.save_guardian_state(state)
            return dict(state)
        except (sqlite3.Error, OSError) as exc:
            # A temporarily unavailable database/destination must not kill the
            # scheduler thread and silently disable all future checks.
            with self._lock:
                self._state = {**self._state, 'status': 'warning',
                               'checked_at': time.time(), 'retry_soon': True,
                               'message': f'Archive health check could not complete: {exc}'}
                return dict(self._state)
        finally:
            db.close_db()
            with self._lock:
                self._running = False

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
            state['running'] = self._running
        return state
