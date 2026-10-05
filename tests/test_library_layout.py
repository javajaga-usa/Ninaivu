"""Where new files land in the library, as it grows.

A live photo is a still and a clip with one stem, and Ninaivu pairs them by
that stem. Every place that renames a file to dodge a name already taken has
to rename both halves the same way, or the still pairs with a stranger's clip
(or with none). These tests put a stranger's file where one half would land
and check the pair still arrives together.
"""

import os
import time

import pytest

from ninaivu import archive
from ninaivu.archive import database as adb
from ninaivu.archive.scanner import ArchiveJob

from test_archive_engine import archived_files, jpeg_with_date, needs_exifread, write

#: The smallest QuickTime file the archive's sniffer calls a video. It carries
#: no recording date, so it is dated by its modification time.
MOV = b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  '


def _clip(path, payload, when='2020-07-04 12:00:00'):
    write(path, MOV + payload)
    stamp = time.mktime(time.strptime(when, '%Y-%m-%d %H:%M:%S'))
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def work(tmp_path):
    archive.configure(tmp_path)
    adb.close_db()
    adb.init_db()
    try:
        yield str(tmp_path)
    finally:
        adb.close_db()


def _day(dest):
    return os.path.join(dest, '2020', '07', '04')


def _landed(dest):
    return sorted(os.path.basename(p) for p in archived_files(dest)
                  if not p.endswith('.json'))


@needs_exifread
def test_a_stranger_still_does_not_take_the_clip(work):
    """Before: the still became IMG_0001_1.jpg and the clip kept IMG_0001.mov,
    which then paired with the stranger's IMG_0001.jpg."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(_day(dest), 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 09:00:00', b'STRANGER' * 100))
    write(os.path.join(src, 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'OURS' * 100))
    _clip(os.path.join(src, 'IMG_0001.mov'), b'CLIP' * 100)

    ArchiveJob([src], dest).run()

    assert _landed(dest) == ['IMG_0001.jpg', 'IMG_0001_1.jpg', 'IMG_0001_1.mov']


@needs_exifread
def test_a_stranger_clip_does_not_take_the_still(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    _clip(os.path.join(_day(dest), 'IMG_0001.mov'), b'STRANGER' * 100)
    write(os.path.join(src, 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'OURS' * 100))
    _clip(os.path.join(src, 'IMG_0001.mov'), b'CLIP' * 100)

    ArchiveJob([src], dest).run()

    assert _landed(dest) == ['IMG_0001.mov', 'IMG_0001_1.jpg', 'IMG_0001_1.mov']


@needs_exifread
def test_pictures_now_and_videos_later_still_pair(work):
    """The halves archived in two runs, one per media type, pick the same name."""
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(_day(dest), 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 09:00:00', b'STRANGER' * 100))
    write(os.path.join(src, 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'OURS' * 100))
    _clip(os.path.join(src, 'IMG_0001.mov'), b'CLIP' * 100)

    ArchiveJob([src], dest, media_types=['image']).run()
    ArchiveJob([src], dest, media_types=['video']).run()

    assert _landed(dest) == ['IMG_0001.jpg', 'IMG_0001_1.jpg', 'IMG_0001_1.mov']


@needs_exifread
def test_a_rerun_keeps_the_pair_where_it_is(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(_day(dest), 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 09:00:00', b'STRANGER' * 100))
    write(os.path.join(src, 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'OURS' * 100))
    _clip(os.path.join(src, 'IMG_0001.mov'), b'CLIP' * 100)

    ArchiveJob([src], dest).run()
    ArchiveJob([src], dest).run()

    assert _landed(dest) == ['IMG_0001.jpg', 'IMG_0001_1.jpg', 'IMG_0001_1.mov']


@needs_exifread
def test_the_dry_run_predicts_the_same_names(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(_day(dest), 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 09:00:00', b'STRANGER' * 100))
    write(os.path.join(src, 'IMG_0001.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'OURS' * 100))
    _clip(os.path.join(src, 'IMG_0001.mov'), b'CLIP' * 100)

    ArchiveJob([src], dest, mode='dry-run').run()

    planned = sorted(os.path.basename(r['destination_path'])
                     for r in adb.get_recent_files(50) if r['status'] == 'planned')
    assert planned == ['IMG_0001_1.jpg', 'IMG_0001_1.mov']


@needs_exifread
def test_an_unpaired_photo_is_named_as_before(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    write(os.path.join(_day(dest), 'IMG_0001.mov'), MOV + b'STRANGER' * 100)
    write(os.path.join(src, 'IMG_0002.jpg'),
          jpeg_with_date('2020:07:04 12:00:00', b'OURS' * 100))

    ArchiveJob([src], dest).run()

    assert 'IMG_0002.jpg' in _landed(dest)


# --- approved uploads and phone backups ------------------------------------

import io  # noqa: E402

from PIL import Image  # noqa: E402

from ninaivu.media import upload_review  # noqa: E402
from ninaivu.storage import db  # noqa: E402

from test_date_access import dates as dates  # noqa: E402,F401

WHEN = time.mktime(time.strptime('2015-06-07 12:30:00', '%Y-%m-%d %H:%M:%S'))


class _Bytes:
    def __init__(self, data):
        self.data = data

    def save(self, output):
        output.write(self.data)


def _jpeg(colour):
    stream = io.BytesIO()
    image = Image.new('RGB', (64, 48), colour)
    exif = image.getexif()
    exif[0x9003] = '2015:06:07 12:30:00'
    image.save(stream, 'JPEG', exif=exif)
    return stream.getvalue()


def _stage(cfg, conn, user, name, data):
    staged = upload_review.stage(conn, cfg, _Bytes(data), name, cfg.active_root,
                                 '', user, mtime=WHEN)
    return staged['id']


def _user(conn, name):
    return conn.execute('SELECT id FROM users WHERE username=?', (name,)).fetchone()['id']


def _folder(cfg):
    return os.path.join(cfg.active_root, '2015', '06', '07')


def _approve(cfg, conn, upload_id, reviewer):
    asset = upload_review.approve(conn, cfg, upload_id, reviewer)
    return asset['rel_path'].rsplit('/', 1)[-1]


def _stems(*names):
    return {os.path.splitext(n)[0] for n in names}


def test_an_uploaded_live_photo_stays_one_pair(dates):
    """Before: each half that found its name taken got its own random suffix,
    so the still and the clip never paired again."""
    cfg, conn, *_ = dates
    reviewer = conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()['id']
    write(os.path.join(_folder(cfg), 'IMG_0001.jpg'), _jpeg('red'))
    _clip(os.path.join(_folder(cfg), 'IMG_0001.mov'), b'STRANGER' * 100)

    still = _stage(cfg, conn, reviewer, 'IMG_0001.jpg', _jpeg('blue'))
    clip = _stage(cfg, conn, reviewer, 'IMG_0001.mov', MOV + b'CLIP' * 100)
    names = (_approve(cfg, conn, still, reviewer), _approve(cfg, conn, clip, reviewer))

    assert len(_stems(*names)) == 1, names
    assert names[0] != 'IMG_0001.jpg'


def test_a_stranger_still_does_not_pair_with_an_uploaded_clip(dates):
    """Only the still's name is taken. The clip, approved first, must not keep
    the plain name and pair with the stranger."""
    cfg, conn, *_ = dates
    reviewer = conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()['id']
    write(os.path.join(_folder(cfg), 'IMG_0001.jpg'), _jpeg('red'))

    still = _stage(cfg, conn, reviewer, 'IMG_0001.jpg', _jpeg('blue'))
    clip = _stage(cfg, conn, reviewer, 'IMG_0001.mov', MOV + b'CLIP' * 100)
    names = (_approve(cfg, conn, clip, reviewer), _approve(cfg, conn, still, reviewer))

    assert len(_stems(*names)) == 1, names
    assert names[0] != 'IMG_0001.mov'


def test_another_persons_upload_is_not_a_companion(dates):
    cfg, conn, *_ = dates
    reviewer = conn.execute('SELECT id FROM users ORDER BY id LIMIT 1').fetchone()['id']
    maya = _user(conn, 'maya')
    write(os.path.join(_folder(cfg), 'IMG_0001.jpg'), _jpeg('red'))

    still = _stage(cfg, conn, reviewer, 'IMG_0001.jpg', _jpeg('blue'))
    theirs = _stage(cfg, conn, maya, 'IMG_0001.mov', MOV + b'CLIP' * 100)
    still_name = _approve(cfg, conn, still, reviewer)
    clip_name = _approve(cfg, conn, theirs, reviewer)

    assert still_name != 'IMG_0001.jpg'
    assert clip_name == 'IMG_0001.mov'


# --- undated files ---------------------------------------------------------

from ninaivu.archive import scanner as archive_scanner  # noqa: E402
from ninaivu.archive.safety import UNDATED_FOLDER  # noqa: E402


def _undated(path, payload):
    write(path, b'\xff\xd8' + payload + b'\xff\xd9')
    os.utime(path, (0, 0))          # an implausible time, so no date at all


def test_undated_files_are_kept_apart_by_the_folder_they_came_from(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    _undated(os.path.join(src, 'WhatsApp Images', 'image.jpg'), b'A' * 300)
    _undated(os.path.join(src, 'Scans', 'image.jpg'), b'B' * 300)

    ArchiveJob([src], dest).run()

    undated = os.path.join(dest, UNDATED_FOLDER)
    assert sorted(os.path.relpath(p, undated) for p in archived_files(dest)) == [
        os.path.join('Scans', 'image.jpg'),
        os.path.join('WhatsApp Images', 'image.jpg')]


def test_an_unusable_folder_name_falls_back_to_the_flat_folder(tmp_path):
    dest = str(tmp_path / 'd')
    flat = os.path.join(dest, UNDATED_FOLDER)
    assert archive_scanner.target_folder(dest, None, os.path.join('/', 'x.jpg')) == flat
    assert archive_scanner.target_folder(dest, None, '/src/.../x.jpg') == flat
    assert archive_scanner.target_folder(dest, None, '/src/CON/x.jpg') == flat
    # The gallery would date a file in a folder named like a date.
    assert archive_scanner.target_folder(dest, None, '/src/2015/x.jpg') == flat
    assert archive_scanner.target_folder(dest, None, '/src/2017-07 Kerala/x.jpg') == flat
    assert archive_scanner.target_folder(dest, None) == flat


def test_files_already_in_the_flat_folder_are_not_moved_or_copied_again(work):
    src, dest = os.path.join(work, 's'), os.path.join(work, 'd')
    _undated(os.path.join(src, 'old', 'image.jpg'), b'A' * 300)
    # What an earlier version left: the same bytes in the flat folder.
    _undated(os.path.join(dest, UNDATED_FOLDER, 'image.jpg'), b'A' * 300)
    ArchiveJob([src], dest).run()
    ArchiveJob([src], dest).run()

    assert archived_files(dest) == [os.path.join(dest, UNDATED_FOLDER, 'image.jpg')]


# --- finding a library that has moved --------------------------------------

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from ninaivu.media.media import thumb_base  # noqa: E402
from ninaivu.server import auth  # noqa: E402
from ninaivu.storage import library_id  # noqa: E402


@pytest.fixture()
def moved(tmp_path):
    """A library indexed at Volumes/Photos/Ninaivu, then mounted as
    "Volumes/Photos 1" — what macOS does when another disk took the name."""
    volumes = tmp_path / 'Volumes'
    old = volumes / 'Photos' / 'Ninaivu'
    (old / '2019' / '07').mkdir(parents=True)
    (old / '2019' / '07' / 'IMG_1.JPG').write_bytes(b'photo')
    state = tmp_path / 'state'
    (state / 'thumbs').mkdir(parents=True)
    conn = db.init_db(state / 'index.db')
    auth.init_auth_schema(conn)
    rel = '2019/07/IMG_1.JPG'
    base = thumb_base(str(old), rel)
    conn.execute("INSERT INTO assets(root, rel_path, filename, kind, thumb) "
                 "VALUES (?,?,?,?,?)", (str(old), rel, 'IMG_1.JPG', 'picture', base))
    conn.commit()
    conn.close()
    shard, _, digest = base.rpartition('/')
    (state / 'thumbs' / shard).mkdir()
    (state / 'thumbs' / shard / f'{digest}_256.webp').write_bytes(b'thumb')
    (state / 'config.json').write_text(json.dumps(
        {'roots': [str(old)], 'active_root': str(old)}), encoding='utf-8')
    cfg = SimpleNamespace(state_dir=state, roots=[str(old)], active_root=str(old))

    library_id.relocate(cfg, mounts=lambda: [])            # first start: marks it
    new = volumes / 'Photos 1' / 'Ninaivu'
    new.parent.mkdir()
    old.rename(new)
    look = lambda root: [volumes / 'Photos 1' / 'Ninaivu', volumes / 'Photos 1']  # noqa: E731
    return cfg, state, old, new, look, rel


def test_a_new_library_folder_gets_a_marker_and_is_remembered(tmp_path):
    root = tmp_path / 'lib'
    root.mkdir()
    cfg = SimpleNamespace(state_dir=tmp_path, roots=[str(root)], active_root=str(root))
    library_id.relocate(cfg, mounts=lambda: [])
    ident = library_id.read_id(root)
    assert ident
    assert json.loads((tmp_path / library_id.KNOWN).read_text()) == {str(root): ident}
    library_id.relocate(cfg, mounts=lambda: [])
    assert library_id.read_id(root) == ident, 'the id must never change'


def test_a_moved_library_is_found_and_rerooted(moved):
    cfg, state, old, new, look, rel = moved
    assert library_id.relocate(cfg, look=look, mounts=lambda: []) == [(str(old), str(new))]
    assert cfg.roots == [str(new)] and cfg.active_root == str(new)
    import sqlite3
    with sqlite3.connect(state / 'index.db') as conn:
        assert conn.execute('SELECT root FROM assets').fetchone()[0] == str(new)
    shard, _, digest = thumb_base(str(new), rel).rpartition('/')
    assert (state / 'thumbs' / shard / f'{digest}_256.webp').read_bytes() == b'thumb'
    assert json.loads((state / 'config.json').read_text())['roots'] == [str(new)]


def test_a_copy_on_a_network_share_is_not_taken_for_the_library(moved):
    """The Pi's backup share carries the same id as the library it copies."""
    cfg, state, old, new, look, _ = moved
    share = [(str(new.parent), 'smbfs')]
    assert library_id.relocate(cfg, look=look, mounts=lambda: share) == []
    assert cfg.roots == [str(old)]


def test_two_folders_with_the_same_id_are_left_for_a_person(moved, tmp_path):
    cfg, state, old, new, look, _ = moved
    copy = tmp_path / 'Volumes' / 'Backup' / 'Ninaivu'
    copy.mkdir(parents=True)
    (copy / library_id.MARKER).write_bytes((new / library_id.MARKER).read_bytes())
    both = lambda root: [new, copy]  # noqa: E731
    assert library_id.relocate(cfg, look=both, mounts=lambda: []) == []
    assert cfg.roots == [str(old)]


def test_a_library_that_is_still_there_is_left_alone(moved):
    cfg, state, old, new, look, _ = moved
    new.rename(old)
    assert library_id.relocate(cfg, look=look, mounts=lambda: []) == []
    assert cfg.roots == [str(old)]


def test_where_to_look_follows_the_disk_the_library_was_on(tmp_path, monkeypatch):
    assert library_id._split_mount('/Volumes/Photos/Ninaivu') == (['/Volumes'], 'Ninaivu')
    assert library_id._split_mount('/media/pi/Ninaivu/lib') == (['/media/pi'], 'lib')
    assert library_id._split_mount('/run/media/me/SSD') == (['/run/media/me'], '')
    assert library_id._split_mount(r'E:\Photo Archive') == ([], 'Photo Archive')
    assert library_id._split_mount('/home/me/Pictures') == ([], '')


def test_the_deepest_mount_decides_whether_a_path_is_on_the_network():
    table = [('/', 'apfs'), ('/Volumes/Ninaivu', 'smbfs')]
    from pathlib import Path
    assert library_id._is_network(Path('/Volumes/Ninaivu/current'), table)
    assert not library_id._is_network(Path('/Volumes/Photos/Ninaivu'), table)
    assert not library_id._is_network(Path('/Volumes/Ninaivu2'), table)
