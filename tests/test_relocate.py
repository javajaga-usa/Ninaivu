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
        self.base = Path(self.temporary.name)
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
