"""Scheduled, read-only sampling of archive integrity and free space."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from typing import Callable

from . import database as db
from .safety import long_path
from .scanner import hash_file

log = logging.getLogger(__name__)


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
        #: The supervisor its loop runs on; Ninaivu gives it its own.
        self.supervisor = None
        #: The wait before the next look, as the last look decided it.
        self._next_look = 300.0
        self._check_thread: threading.Thread | None = None
        self._running = False
        self._state = db.load_guardian_state()

    #: The thread it runs on, as before it moved to the supervisor.
    JOB = 'archive-guardian'

    def start(self) -> None:
        # Check shortly after boot, then every five minutes (or sooner, with
        # a shorter interval) for whether the daily run is due. Kept by the
        # supervisor (server/supervisor.py); _look keeps its own failures.
        from ..server.supervisor import Job, every, supervisor_of  # noqa: PLC0415
        supervisor_of(self).add(Job(
            self.JOB, self._look, every(lambda: min(300, max(5, self._next_look)), first=5),
            log=log, retry_most=0,
            progress=lambda: {'status': self._state.get('status'),
                              'checking': self._running}))

    def stop(self, timeout: float = 3.0) -> None:
        from ..server.supervisor import supervisor_of            # noqa: PLC0415
        supervisor_of(self).stop_job(self.JOB, timeout)

    def _look(self) -> None:
        """One look: run the daily check if it is due and the archive is idle."""
        retry = 300
        try:
            last = float(self._state.get('checked_at') or 0)
            # Soon for what may put itself right within minutes: a check that
            # could not finish, an unplugged drive, and a nearly full disk
            # (for its space only: sample_due keeps the read-back daily).
            retry = 300 if (self._state.get('retry_soon')
                            or self._state.get('status') in ('no-archive', 'warning')) \
                else self.interval
            if time.time() - last >= retry and not self.is_archive_running():
                self.run_once(sample=self.sample_due())
        except Exception:                                   # noqa: BLE001
            # run_once keeps the database and the disk to themselves;
            # anything else (a damaged row, a bad setting) ended this
            # thread, and with it every daily check, without a word.
            log.exception('archive health check failed; trying again in 5 minutes')
            with self._lock:
                self._state = {**self._state, 'retry_soon': True}
            retry = 300
        self._next_look = retry

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
