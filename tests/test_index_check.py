"""Checking the index's databases at start, and repairing a damaged one.

The databases here are damaged for real: pages of an index or of a table
overwritten with noise, as a failing disk or a bad write leaves them.
"""
import logging
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ninaivu.server import runfile
from ninaivu.storage import index_check, relocate


def _has_dbstat() -> bool:
    try:
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('SELECT * FROM dbstat LIMIT 1')
        return True
    except sqlite3.Error:
        return False


DBSTAT = _has_dbstat()


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name).resolve()
        quiet = patch.object(index_check, '_say')
        self.said = quiet.start()
        self.addCleanup(quiet.stop)

    def make(self, name='index.db', rows=20000):
        path = self.state / name
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("""
                CREATE TABLE assets(id INTEGER PRIMARY KEY, rel_path TEXT, caption TEXT);
                CREATE INDEX assets_path ON assets(rel_path);
                CREATE VIRTUAL TABLE assets_fts USING fts5(caption, content='assets', content_rowid='id');
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TRIGGER assets_ai AFTER INSERT ON assets BEGIN
                    INSERT INTO assets_fts(rowid, caption) VALUES (new.id, new.caption); END;
            """)
            conn.executemany('INSERT INTO assets(rel_path, caption) VALUES (?, ?)',
                             [(f'2024/{i:06d}.jpg', f'photo {i} on the beach') for i in range(rows)])
            conn.execute('PRAGMA user_version = 42')
            conn.commit()
        return path

    def damage(self, path, what, pages=3):
        with closing(sqlite3.connect(path)) as conn:
            leaves = [row[0] for row in conn.execute(
                "SELECT pageno FROM dbstat WHERE name = ? AND pagetype = 'leaf'", (what,))]
            size = conn.execute('PRAGMA page_size').fetchone()[0]
        with path.open('r+b') as stream:
            for page in leaves[len(leaves) // 3: len(leaves) // 3 + pages]:
                stream.seek((page - 1) * size + 100)
                stream.write(os.urandom(size // 4))

    def count(self, path, sql='SELECT count(*) FROM assets'):
        with closing(sqlite3.connect(path)) as conn:
            return conn.execute(sql).fetchone()[0]


class Checking(Base):
    def test_a_sound_index_is_checked_and_left_alone(self):
        path = self.make()
        before = path.read_bytes()
        index_check.ask(self.state, 'moved')
        result = index_check.run(self.state)
        self.assertTrue(result['ok'])
        self.assertEqual(result['repaired'], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(index_check.wanted(self.state))
        self.assertEqual(index_check.status(self.state)['last']['reason'], 'moved')
        self.assertEqual(list(self.state.glob('*.damaged-*')), [])

    def test_nothing_asks_nothing_is_checked(self):
        self.make()
        self.assertIsNone(index_check.wanted(self.state))

    def test_the_list_of_databases_is_the_backups(self):
        from ninaivu.storage import backup
        self.assertEqual(index_check.DATABASES, backup.DATABASES)


@unittest.skipUnless(DBSTAT, 'needs SQLite with dbstat to aim the damage')
class Repairing(Base):
    def test_a_damaged_index_is_rebuilt_with_every_row(self):
        path = self.make()
        self.damage(path, 'assets_path')
        self.assertTrue(index_check.check(path))
        result = index_check.run(self.state)
        self.assertTrue(result['ok'])
        self.assertEqual(result['repaired'], ['index.db'])
        self.assertEqual(index_check.check(path), [])
        self.assertEqual(self.count(path), 20000)
        self.assertEqual(self.count(path, "SELECT count(*) FROM assets_fts WHERE assets_fts MATCH 'beach'"), 20000)
        self.assertEqual(self.count(path, 'PRAGMA user_version'), 42)
        # The trigger is back: a photograph added later is found by search.
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("INSERT INTO assets(rel_path, caption) VALUES ('new.jpg', 'a heron')")
            conn.commit()
        self.assertEqual(self.count(path, "SELECT count(*) FROM assets_fts WHERE assets_fts MATCH 'heron'"), 1)

    def test_a_damaged_table_keeps_what_can_be_read(self):
        path = self.make()
        self.damage(path, 'assets')
        result = index_check.run(self.state)
        database = result['databases'][0]
        self.assertTrue(result['ok'])
        self.assertEqual(database['how'], 'rebuild')
        self.assertGreaterEqual(database['skipped'], 1)
        self.assertEqual(index_check.check(path), [])
        kept = self.count(path)
        self.assertGreater(kept, 19000)
        self.assertLess(kept, 20000)
        # Ids are kept: the thumbnails and faces that name them still match.
        self.assertEqual(self.count(path, 'SELECT max(id) FROM assets'), 20000)

    def test_the_damaged_original_is_kept_and_only_the_newest(self):
        path = self.make()
        old = self.state / 'index.db.damaged-20200101-000000'
        old.mkdir()
        (old / 'index.db').write_bytes(b'an older damaged copy')
        self.damage(path, 'assets_path')
        damaged = path.read_bytes()
        result = index_check.run(self.state)
        aside = Path(result['databases'][0]['damaged_copy'])
        self.assertEqual((aside / 'index.db').read_bytes(), damaged)
        self.assertFalse(old.exists())

    def test_the_photo_search_is_rebuilt_again_once_ninaivu_runs(self):
        from ninaivu.storage.db import FTS_REBUILD_KEY
        path = self.make()
        self.damage(path, 'assets')
        index_check.run(self.state)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute('SELECT value FROM meta WHERE key = ?',
                                          (FTS_REBUILD_KEY,)).fetchone(), ('1',))


class CannotBeRepaired(Base):
    def test_a_file_that_is_not_a_database_is_left_as_it_was(self):
        path = self.state / 'index.db'
        path.write_bytes(b'not a database at all' * 500)
        before = path.read_bytes()
        result = index_check.run(self.state)
        self.assertFalse(result['ok'])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(result['databases'][0]['left'])
        self.assertTrue(Path(result['databases'][0]['damaged_copy']).is_dir())


@unittest.skipUnless(DBSTAT, 'needs SQLite with dbstat to aim the damage')
class Safeguards(Base):
    def test_without_room_for_a_safe_repair_nothing_is_changed(self):
        path = self.make()
        self.damage(path, 'assets')
        before = path.read_bytes()
        with patch.object(index_check.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            result = index_check.run(self.state)
        self.assertFalse(result['ok'])
        self.assertIn('not enough free space', result['databases'][0]['left'][0])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.state.glob('*.damaged-*')), [])

    def test_a_copy_set_aside_that_does_not_match_stops_the_repair(self):
        path = self.make()
        self.damage(path, 'assets')
        before = path.read_bytes()
        real = index_check._sha256
        with patch.object(index_check, '_sha256',
                          side_effect=lambda p: b'other' if '.damaged-' in str(p) else real(p)):
            result = index_check.run(self.state)
        self.assertFalse(result['ok'])
        self.assertEqual(path.read_bytes(), before)

    def test_a_repaired_database_passes_the_full_integrity_check(self):
        path = self.make()
        self.damage(path, 'assets_path')
        index_check.run(self.state)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchall(), [('ok',)])

    def test_rows_that_lost_their_parent_are_counted(self):
        path = self.state / 'index.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("""
                CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE faces(id INTEGER PRIMARY KEY, person INTEGER REFERENCES people(id), crop TEXT);
            """)
            conn.executemany('INSERT INTO people(name) VALUES (?)', [(f'person {i} ' * 20,) for i in range(5000)])
            conn.executemany('INSERT INTO faces(person, crop) VALUES (?, ?)', [(i % 5000 + 1, 'c') for i in range(5000)])
            conn.commit()
        self.damage(path, 'people')
        result = index_check.run(self.state)
        self.assertTrue(result['ok'])
        self.assertGreater(result['databases'][0]['orphans'], 0)


class Watching(Base):
    def test_a_logged_damage_asks_for_a_check_once(self):
        watch = index_check.Watch(self.state)
        logger = logging.getLogger('ninaivu.test.describer')
        logger.addHandler(watch)
        self.addCleanup(logger.removeHandler, watch)
        logger.propagate = False
        self.addCleanup(setattr, logger, 'propagate', True)
        try:
            raise sqlite3.DatabaseError('database disk image is malformed')
        except sqlite3.DatabaseError:
            logger.exception('Describing failed')
        wanted = index_check.wanted(self.state)
        self.assertEqual(wanted['reason'], 'error')
        self.assertIn('malformed', wanted['detail'])
        logger.error('database disk image is malformed again')
        self.assertEqual(index_check.wanted(self.state)['at'], wanted['at'])

    def test_ordinary_errors_ask_for_nothing(self):
        watch = index_check.Watch(self.state)
        watch.emit(logging.LogRecord('x', logging.ERROR, __file__, 1, 'database is locked', None, None))
        self.assertIsNone(index_check.wanted(self.state))

    def test_watching_is_attached_once(self):
        root = logging.getLogger()
        before = list(root.handlers)
        self.addCleanup(lambda: setattr(root, 'handlers', before))
        index_check.watch(self.state)
        index_check.watch(self.state)
        self.assertEqual(sum(isinstance(h, index_check.Watch) for h in root.handlers), 1)


class AfterAMove(Base):
    def test_a_move_has_the_new_folder_checked_at_the_start(self):
        source = self.state / 'old'
        source.mkdir()
        shutil.move(str(self.make()), source / 'index.db')
        target = self.state / 'new'
        with patch.object(relocate, '_say'):
            relocate.request(source, target)
            with runfile.starting_lock(source):
                self.assertEqual(relocate.apply_pending(source), target)
        self.assertEqual(index_check.wanted(target)['reason'], 'moved')
        self.assertIsNone(index_check.wanted(source))

    def test_the_control_panel_waits_for_a_long_repair(self):
        from ninaivu import __main__ as main
        from ninaivu.desktop import control
        told = []
        path = self.make()
        if DBSTAT:
            self.damage(path, 'assets')
        index_check.run(self.state, tell=lambda what, *_counts: told.append(what))
        self.assertTrue(told[0].startswith(main.CHECK_STEP))
        if DBSTAT:
            self.assertTrue(any(step.startswith(control.REPAIR_STEP) for step in told))


class Console(Base):
    def test_asking_from_the_console_checks_at_a_restart(self):
        from flask import Flask
        from ninaivu.api import migration_api, server_api
        app = Flask(__name__)
        app.config['MV_CONFIG'] = SimpleNamespace(state_dir=self.state)
        with app.test_request_context(json={}), \
                patch.object(server_api, '_restart_problem', return_value=None), \
                patch.object(server_api, '_begin_restart', return_value=({}, 202)) as restart:
            response, status = migration_api.check_index.__wrapped__()
        self.assertEqual(status, 202)
        self.assertTrue(response.get_json()['restarting'])
        restart.assert_called_once()
        self.assertEqual(index_check.wanted(self.state)['reason'], 'asked')


if __name__ == '__main__':
    unittest.main()
