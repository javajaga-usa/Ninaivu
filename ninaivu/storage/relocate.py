"""Relocate a closed state folder; leave the original as a recovery copy."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import ExitStack
from pathlib import Path

REDIRECT = 'index-location.json'
PENDING = 'index-move-pending.json'
RESULT = 'index-move-result.json'
MANIFEST = 'index-recovery-manifest.json'


def _destination(record: object) -> Path:
    destination = record.get('destination') if isinstance(record, dict) else None
    if not isinstance(destination, str) or not destination or not Path(destination).is_absolute():
        raise ValueError('The saved index location must name an absolute folder path.')
    return Path(destination)


def resolve(folder: Path) -> Path:
    seen = set()
    folder = folder.expanduser().resolve()
    while (folder / REDIRECT).exists():
        if folder in seen:
            raise ValueError('The saved index locations contain a loop.')
        seen.add(folder)
        record = json.loads((folder / REDIRECT).read_text(encoding='utf-8'))
        folder = _destination(record).resolve()
    # Intermediate folders may contain only their forwarding note after the
    # recovery data has been removed. Only the final destination needs an index.
    if seen and not (folder / 'index.db').is_file():
        raise ValueError('The relocated index is unavailable. '
                         'Reconnect its drive before starting Ninaivu.')
    return folder


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def validate(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = destination.expanduser()
    if not destination.is_absolute():
        raise ValueError('Choose an absolute folder path on the Ninaivu computer.')
    destination = destination.resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError('Choose a folder outside the current index folder.')
    if destination.exists():
        raise ValueError('Choose a new folder that does not already exist.')
    if not destination.parent.is_dir():
        raise ValueError('The parent of the new folder must already exist.')
    return destination


def request(source: Path, destination: Path) -> None:
    destination = validate(source, destination)
    if not (source / 'index.db').is_file():
        raise ValueError('There is no index to move.')
    if (source / 'import-pending.json').exists() or (source / PENDING).exists():
        raise ValueError('An index import or move is already waiting. Finish it first.')
    write(source / PENDING, {'destination': str(destination)})


def _digest(path: Path) -> bytes:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').digest()


def apply_pending(source: Path, *, starts: ExitStack | None = None) -> Path | None:
    """Called before opening any database, with the source starting lock held.

    A server passes its startup stack so the destination stays locked until
    it acquires the server lock. Otherwise a second start could open the new
    folder between publication and the original start's handover.
    """
    marker = source / PENDING
    if not marker.exists():
        return None
    from ..server import runfile
    destination = None
    created = False
    published = False
    try:
        destination = validate(source, _destination(json.loads(marker.read_text(encoding='utf-8'))))
        with runfile.server_lock(source), ExitStack() as copying:
            (starts if starts is not None else copying).enter_context(
                runfile.starting_lock(destination))
            # mkdir is exclusive: never copy over somebody else's files.
            destination.mkdir(mode=0o700)
            created = True
            excluded = {PENDING, REDIRECT, MANIFEST, 'ninaivu-server.lock', 'ninaivu-starting.lock',
                        runfile.RUN_FILE, runfile.RUN_FILE + '.part',
                        'ninaivu.starting', f'ninaivu.starting.{os.getpid()}.tmp'}
            for item in source.rglob('*'):
                if item.is_symlink():
                    raise ValueError('The index folder contains a symbolic link. Move it manually.')
            size = sum(p.stat().st_size for p in source.rglob('*')
                       if p.relative_to(source).parts[0] not in excluded and p.is_file())
            if shutil.disk_usage(destination).free < size + 64 * 1024 * 1024:
                raise ValueError('The destination does not have enough free space.')
            for item in source.iterdir():
                if item.name in excluded:
                    continue
                target = destination / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            recovery_files = {}
            for item in source.rglob('*'):
                relative = item.relative_to(source)
                if relative.parts[0] not in excluded and item.is_file():
                    digest = _digest(item)
                    if digest != _digest(destination / relative):
                        raise ValueError(f'The copied file could not be verified: {relative}')
                    recovery_files[relative.as_posix()] = {'sha256': digest.hex(),
                                                         'size': item.stat().st_size}
            # Config may contain the old state_dir. Rewrite it before publishing.
            config = destination / 'config.json'
            if config.exists():
                data = json.loads(config.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('The index settings must contain a JSON object.')
                data['state_dir'] = str(destination)
                write(config, data)
            write(destination / MANIFEST, {'source': str(source), 'destination': str(destination),
                                           'files': recovery_files})
            write(destination / RESULT, {'ok': True, 'source': str(source),
                                         'destination': str(destination),
                                         'recovery_files': len(recovery_files),
                                         'recovery_bytes': sum(f['size'] for f in recovery_files.values())})
            write(source / REDIRECT, {'destination': str(destination)})
            published = True
            marker.unlink(missing_ok=True)
            return destination
    except (OSError, ValueError, KeyError, runfile.AlreadyRunning) as error:
        # A failed attempt keeps the current library usable and reports why.
        if published:
            return destination
        if created and destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
        marker.unlink(missing_ok=True)
        write(source / RESULT, {'ok': False, 'error': str(error)})
        return None
