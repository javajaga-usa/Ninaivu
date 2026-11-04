"""Closed-index moves: publish only verified copies, preserve recovery data."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ninaivu.storage import relocate
from ninaivu.server import runfile


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Resolved, as Config.load gives the state folder: on Windows the
        # temporary folder is an 8.3 name (RUNNER~1), on a Mac /var is
        # /private/var, and relocate.resolve() returns the real path.
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / 'old'
        self.source.mkdir()
        (self.source / 'index.db').write_bytes(b'index contents')
        (self.source / 'config.json').write_text(json.dumps(
            {'state_dir': str(self.source), 'roots': ['/photos']}))
        (self.source / 'thumbs').mkdir()
        (self.source / 'thumbs' / 'face.jpg').write_bytes(b'face')
        self.target = self.base / 'new folder'

    def test_verified_move_and_future_launches(self):
        relocate.request(self.source, self.target)
        with runfile.starting_lock(self.source):
            self.assertEqual(relocate.apply_pending(self.source), self.target)
        self.assertEqual(relocate.resolve(self.source), self.target)
        self.assertEqual((self.target / 'thumbs/face.jpg').read_bytes(), b'face')
        copied = json.loads((self.target / 'config.json').read_text())
        self.assertEqual(copied['state_dir'], str(self.target))
        self.assertTrue((self.source / 'index.db').exists())
        self.assertFalse((self.target / relocate.PENDING).exists())

    def test_refuse_existing_nested_and_relative_destinations(self):
        for target in [self.source, self.base, self.source / 'nested', Path('relative')]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                relocate.request(self.source, target)

    def test_failed_verification_keeps_original(self):
        relocate.request(self.source, self.target)
        with patch.object(relocate, '_digest', side_effect=[b'a', b'b']):
            self.assertIsNone(relocate.apply_pending(self.source))
        self.assertFalse(self.target.exists())
        self.assertEqual(relocate.resolve(self.source), self.source)
        self.assertFalse(json.loads((self.source / relocate.RESULT).read_text())['ok'])

    def test_running_server_cannot_be_copied(self):
        relocate.request(self.source, self.target)
        with runfile.server_lock(self.source):
            self.assertIsNone(relocate.apply_pending(self.source))
        self.assertFalse(self.target.exists())

    def test_missing_drive_does_not_start_empty_library(self):
        relocate.write(self.source / relocate.REDIRECT, {'destination': str(self.target)})
        with self.assertRaises(ValueError):
            relocate.resolve(self.source)

    def test_destination_created_after_request_is_preserved(self):
        relocate.request(self.source, self.target)
        self.target.mkdir()
        (self.target / 'keep').write_text('mine')
        self.assertIsNone(relocate.apply_pending(self.source))
        self.assertEqual((self.target / 'keep').read_text(), 'mine')

    def test_config_follows_saved_location(self):
        from ninaivu.server.config import Config
        relocate.request(self.source, self.target)
        relocate.apply_pending(self.source)
        with patch.dict('os.environ', {'NINAIVU_STATE_DIR': str(self.source)}):
            self.assertEqual(Config.load().state_dir, self.target)

    def test_console_schedules_move_with_manual_restart(self):
        from flask import Flask
        from ninaivu.api import migration_api, server_api
        from types import SimpleNamespace
        app = Flask(__name__)
        app.config['MV_CONFIG'] = SimpleNamespace(state_dir=self.source)
        with app.test_request_context(json={'folder': str(self.target)}), \
                patch.object(server_api, '_restart_problem', return_value='Restart manually.'), \
                patch.object(server_api, '_begin_restart') as restart:
            response, status = migration_api.move_index.__wrapped__()
        self.assertEqual(status, 202)
        self.assertFalse(response.get_json()['restarting'])
        self.assertTrue((self.source / relocate.PENDING).exists())
        restart.assert_not_called()

    def test_console_rejects_invalid_destination_without_restart(self):
        from flask import Flask
        from ninaivu.api import migration_api, server_api
        from types import SimpleNamespace
        app = Flask(__name__)
        app.config['MV_CONFIG'] = SimpleNamespace(state_dir=self.source)
        with app.test_request_context(json={'folder': str(self.source)}), \
                patch.object(server_api, '_begin_restart') as restart:
            response, status = migration_api.move_index.__wrapped__()
        self.assertEqual(status, 400)
        self.assertIn('error', response.get_json())
        restart.assert_not_called()

    def test_original_launcher_follows_two_moves_after_recovery_cleanup(self):
        relocate.request(self.source, self.target)
        relocate.apply_pending(self.source)
        final = self.base / 'final'
        relocate.request(self.target, final)
        relocate.apply_pending(self.target)
        (self.target / 'index.db').unlink()
        self.assertEqual(relocate.resolve(self.source), final)

    def test_run_record_reader_follows_relocation(self):
        relocate.request(self.source, self.target)
        relocate.apply_pending(self.source)
        runfile.write(self.target, port=5000, admin_port=3000, token='new-server')
        self.assertEqual(runfile.read(self.source)['token'], 'new-server')

    def test_running_panel_refreshes_its_state_folder(self):
        from ninaivu.desktop.control import Controller
        from types import SimpleNamespace
        controller = Controller.__new__(Controller)
        controller.cfg = SimpleNamespace(state_dir=self.source)
        relocate.request(self.source, self.target)
        relocate.apply_pending(self.source)
        controller.record()
        self.assertEqual(controller.cfg.state_dir, self.target)

    def test_progress_moves_with_index_and_keeps_original_start_time(self):
        from ninaivu.server import starting
        starting.end()
        self.addCleanup(starting.end)
        starting.begin(self.source)
        since = starting.read(self.source)['since']
        relocate.request(self.source, self.target)
        relocate.apply_pending(self.source)
        starting.move_to(self.target)
        self.assertEqual(starting.read(self.source)['since'], since)
        self.assertEqual(starting.read(self.target)['since'], since)
        self.assertFalse((self.source / starting.STARTING_FILE).exists())
        starting.end()
        self.assertFalse((self.target / starting.STARTING_FILE).exists())

    def test_malformed_pending_note_keeps_library_usable(self):
        for record in [[], {'destination': 42}, {'destination': None}]:
            with self.subTest(record=record):
                relocate.write(self.source / relocate.PENDING, record)
                self.assertIsNone(relocate.apply_pending(self.source))
                self.assertTrue((self.source / 'index.db').exists())
                self.assertFalse((self.source / relocate.PENDING).exists())
                self.assertFalse(json.loads((self.source / relocate.RESULT).read_text())['ok'])

    def test_stale_run_record_is_not_copied_to_destination(self):
        runfile.write(self.source, port=5000, admin_port=3000, token='old-server')
        relocate.request(self.source, self.target)
        relocate.apply_pending(self.source)
        self.assertIsNone(runfile.read(self.target))

    def test_cycle_is_refused_even_when_recovery_indexes_are_gone(self):
        self.target.mkdir()
        relocate.write(self.source / relocate.REDIRECT, {'destination': str(self.target)})
        relocate.write(self.target / relocate.REDIRECT, {'destination': str(self.source)})
        with self.assertRaisesRegex(ValueError, 'loop'):
            relocate.resolve(self.source)

    def test_invalid_config_does_not_publish_destination(self):
        (self.source / 'config.json').write_text('[]')
        relocate.request(self.source, self.target)
        self.assertIsNone(relocate.apply_pending(self.source))
        self.assertFalse(self.target.exists())
        self.assertFalse((self.source / relocate.REDIRECT).exists())

    def test_destination_start_lock_is_held_until_caller_opens_index(self):
        from contextlib import ExitStack
        relocate.request(self.source, self.target)
        with ExitStack() as starts:
            starts.enter_context(runfile.starting_lock(self.source))
            self.assertEqual(relocate.apply_pending(self.source, starts=starts), self.target)
            with self.assertRaises(runfile.AlreadyStarting), runfile.starting_lock(self.target):
                self.fail('A competing start acquired the destination during handover')
        with runfile.starting_lock(self.target):
            pass
