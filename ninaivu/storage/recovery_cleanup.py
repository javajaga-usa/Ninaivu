"""Reclaim only unchanged recovery files, keeping the original forwarding note."""
from __future__ import annotations

import json
import threading
from pathlib import Path, PurePosixPath

from . import relocate
from ..utils.files import sha256_file

RESULT = 'index-recovery-cleanup.json'


def _manifest(state: Path) -> tuple[Path, dict]:
    data = json.loads((state / relocate.MANIFEST).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('files'), dict):
        raise ValueError('No verified recovery-file list is available for this index.')
    source = Path(data['source'])
    state = state.resolve()
    if (not source.is_absolute() or source.resolve() != source or source == state
            or source in state.parents or state in source.parents
            or relocate.resolve(source) != state):
        raise ValueError('The recovery folder does not forward to this index. Nothing was removed.')
    for name, meta in data['files'].items():
        relative = PurePosixPath(name)
        if (not name or not relative.parts or relative.is_absolute() or '..' in relative.parts or '\\' in name
                or ':' in name or not isinstance(meta, dict)
                or not isinstance(meta.get('sha256'), str)
                or type(meta.get('size')) is not int
                or relative.parts[0] in {relocate.REDIRECT, relocate.MANIFEST, relocate.PENDING,
                                         'ninaivu-server.lock', 'ninaivu-starting.lock'}):
            raise ValueError('The recovery-file list is invalid. Nothing was removed.')
    return source, data['files']


def preview(state: Path) -> dict:
    source, files = _manifest(state)
    return {'source': str(source), 'files': len(files),
            'bytes': sum(meta['size'] for meta in files.values())}


def clean(state: Path, progress=lambda _result: None) -> dict:
    from ..server import runfile
    source, files = _manifest(state)
    result = {'removed': 0, 'bytes': 0, 'preserved': 0, 'source': str(source)}
    with runfile.starting_lock(source), runfile.server_lock(source):
        _manifest(state)  # Recheck ownership under the locks before removing anything.
        for name, meta in files.items():
            path = source / name
            # Added files are absent from the manifest; changed or linked files stay.
            if path.is_symlink() or path.resolve() != path:
                result['preserved'] += 1
                continue
            if not path.exists():
                continue  # Safe to repeat after an interrupted cleanup.
            if (not path.is_file() or path.stat().st_size != meta['size']
                    or sha256_file(path) != meta['sha256']):
                result['preserved'] += 1
                continue
            path.unlink()
            result['removed'] += 1
            result['bytes'] += meta['size']
            progress(dict(result))
        # Only try directories that held verified files, and only when empty.
        parents = {parent for name in files for parent in (source / name).parents
                   if parent != source and source in parent.parents}
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            if parent.is_symlink():
                continue
            try:
                parent.rmdir()
            except OSError:
                pass
    relocate.write(state / RESULT, result)
    return result


class Cleaner:
    def __init__(self):
        self._lock = threading.Lock()
        self._job = {}
        self._state = None
        self._thread = None

    def status(self, state: Path) -> dict:
        with self._lock:
            if self._state == state and self._job:
                return dict(self._job)
        try:
            result = json.loads((state / RESULT).read_text(encoding='utf-8'))
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            return {}

    def start(self, state: Path) -> None:
        details = preview(state)
        with self._lock:
            if self._job.get('running'):
                raise ValueError('Recovery cleanup is already running.')
            self._state = state
            self._job = {**details, 'running': True, 'removed': 0}
        self._thread = threading.Thread(target=self._run, args=(state,), daemon=True,
                                        name='ninaivu-recovery-cleanup')
        self._thread.start()

    def _run(self, state):
        def update(result):
            with self._lock:
                self._job.update(result)
        try:
            update(clean(state, update))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            update({'error': str(error)})
        finally:
            update({'running': False})
