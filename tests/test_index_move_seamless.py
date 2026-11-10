"""The index move from the console, made to work on a real disk (5 Nov 2026).

On the Linux laptop a move stopped at one file that read back differently,
deleted everything it had copied, and said nothing in the log; a folder made
beforehand, or a copy made by hand, was refused; the start note showed no
progress, and the Control Panel gave up on a long move after an hour. After a
move, a later start took its models from the recovery copy, and the console
showed records that belonged to another folder.
"""
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ninaivu.server import runfile
from ninaivu.storage import relocate


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / 'old'
        self.source.mkdir()
        (self.source / 'index.db').write_bytes(b'index contents' * 100)
        (self.source / 'config.json').write_text(json.dumps(
            {'state_dir': str(self.source), 'roots': ['/photos']}))
        (self.source / 'thumbs' / 'ab').mkdir(parents=True)
        (self.source / 'thumbs' / 'ab' / 'face.jpg').write_bytes(b'face' * 50)
        (self.source / 'renditions').mkdir()
        (self.source / 'renditions' / '323699-v1.jpg').write_bytes(b'rendition' * 30)
        self.target = self.base / 'new'
        quiet = patch.object(relocate, '_say')
        self.said = quiet.start()
        self.addCleanup(quiet.stop)

    def move(self, **kwargs):
        relocate.request(self.source, self.target)
        with runfile.starting_lock(self.source):
            return relocate.apply_pending(self.source, **kwargs)

    def result(self, folder):
        return json.loads((folder / relocate.RESULT).read_text())

    def logged(self):
        return ' '.join(str(call.args[0]) for call in self.said.call_args_list)


class ReadingBack(Base):
    def test_a_file_that_reads_back_wrong_once_is_copied_again(self):
        real, bad = relocate._read_back, []

        def flaky(path):
            if path.name == '323699-v1.jpg' and not bad:
                bad.append(path)
                return b'garbled'
            return real(path)

        with patch.object(relocate, '_read_back', flaky):
            self.assertEqual(self.move(), self.target)
        self.assertEqual(relocate.resolve(self.source), self.target)
        self.assertEqual(self.result(self.target)['copied_again'], ['renditions/323699-v1.jpg'])
        self.assertIn('copying it again', self.logged())

    def test_a_copy_that_keeps_coming_back_wrong_blames_the_new_drive(self):
        real = relocate._read_back
        with patch.object(relocate, '_read_back',
                          lambda p: b'garbled' if p.name == '323699-v1.jpg' else real(p)):
            self.assertIsNone(self.move())
        result = self.result(self.source)
        self.assertIn('keeps coming back different', result['error'])
        self.assertIn(str(self.target), result['error'])
        self.assertEqual(result['destination'], str(self.target))
        self.assertEqual(relocate.resolve(self.source), self.source)

    def test_an_original_that_reads_differently_each_time_is_named(self):
        real, reads = relocate._hash_copy, []

        def unsteady(original, copy):
            digest = real(original, copy)
            if original.name == '323699-v1.jpg':
                reads.append(1)
                return digest if len(reads) == 1 else b'another' * 4
            return digest

        real_back = relocate._read_back
        with patch.object(relocate, '_hash_copy', unsteady), \
                patch.object(relocate, '_read_back',
                             lambda p: b'x' if p.name == '323699-v1.jpg' else real_back(p)):
            self.assertIsNone(self.move())
        self.assertIn('original file reads differently', self.result(self.source)['error'])

    def test_the_next_attempt_continues_from_what_was_copied(self):
        real = relocate._read_back
        with patch.object(relocate, '_read_back',
                          lambda p: b'garbled' if p.name == '323699-v1.jpg' else real(p)):
            self.assertIsNone(self.move())
        self.assertTrue((self.target / 'thumbs/ab/face.jpg').exists())
        self.assertTrue(self.result(self.source)['kept_copy'])
        copied = []
        real_copy = relocate._hash_copy
        with patch.object(relocate, '_hash_copy',
                          lambda a, b: copied.append(a.name) or real_copy(a, b)):
            self.assertEqual(self.move(), self.target)
        self.assertEqual(copied, [], 'every file was already there and right')
        self.assertEqual(self.result(self.target)['already_there'], 4)
        self.assertFalse((self.target / relocate.PARTIAL).exists())

    def test_a_failure_before_anything_is_copied_leaves_no_folder(self):
        relocate.request(self.source, self.target)
        with runfile.server_lock(self.source):
            self.assertIsNone(relocate.apply_pending(self.source))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.result(self.source)['kept_copy'])


class Destinations(Base):
    def test_an_empty_folder_made_beforehand_is_used(self):
        self.target.mkdir()
        self.assertEqual(self.move(), self.target)
        self.assertEqual(relocate.resolve(self.source), self.target)

    def test_a_folder_with_other_files_is_refused_before_restarting(self):
        self.target.mkdir()
        (self.target / 'holiday.jpg').write_bytes(b'mine')
        with self.assertRaisesRegex(ValueError, 'already holds other files'):
            relocate.request(self.source, self.target)
        self.assertFalse((self.source / relocate.PENDING).exists())

    def test_a_file_is_refused(self):
        self.target.write_text('not a folder')
        with self.assertRaisesRegex(ValueError, 'not a folder'):
            relocate.request(self.source, self.target)

    def test_a_copy_made_by_hand_is_checked_and_completed(self):
        shutil.copytree(self.source, self.target)       # times kept, as cp -a does
        # Left by a copy taken while Ninaivu ran: must not reach the index.
        (self.target / 'index.db-wal').write_bytes(b'stale journal')
        (self.target / 'ninaivu.run').write_text('{"pid": 1}')
        # Damaged in the copy, but the same size and time as the original.
        face = self.target / 'thumbs/ab/face.jpg'
        stat = face.stat()
        face.write_bytes(b'FACE' * 50)
        os.utime(face, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.move(), self.target)
        self.assertFalse((self.target / 'index.db-wal').exists())
        self.assertIsNone(runfile.read(self.target))
        self.assertEqual(face.read_bytes(), b'face' * 50)
        result = self.result(self.target)
        self.assertEqual(result['copied_again'], ['thumbs/ab/face.jpg'])
        self.assertEqual(result['copied_bytes'], 0)

    def test_moving_back_to_the_recovery_copy(self):
        self.assertEqual(self.move(), self.target)
        (self.target / 'index.db').write_bytes(b'newer contents')
        relocate.request(self.target, self.source)
        with runfile.starting_lock(self.target):
            self.assertEqual(relocate.apply_pending(self.target), self.source)
        self.assertEqual(relocate.resolve(self.source), self.source)
        self.assertEqual(relocate.resolve(self.target), self.source)
        self.assertEqual((self.source / 'index.db').read_bytes(), b'newer contents')

    def test_a_folder_it_cannot_write_in_is_refused_before_restarting(self):
        self.target.mkdir()
        with patch.object(relocate.tempfile, 'mkstemp', side_effect=PermissionError(13, 'denied')), \
                self.assertRaisesRegex(ValueError, 'cannot write'):
            relocate.request(self.source, self.target)
        self.assertFalse((self.source / relocate.PENDING).exists())

    def test_too_little_space_is_refused_before_restarting(self):
        with patch.object(relocate.shutil, 'disk_usage', return_value=SimpleNamespace(free=10)), \
                self.assertRaisesRegex(ValueError, 'enough free space'):
            relocate.request(self.source, self.target)

    @unittest.skipIf(os.name == 'nt', 'links need privileges on Windows')
    def test_a_link_is_refused_before_restarting(self):
        (self.source / 'ai-models').symlink_to(self.base)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            relocate.request(self.source, self.target)


class Progress(Base):
    def test_progress_runs_through_copying_then_verifying(self):
        told = []
        self.move(progress=lambda stage, done, total: told.append((stage, done, total)))
        total = told[-1][2]
        self.assertEqual(told[0], ('copying', 0, total))
        self.assertIn(('copying', total, total), told)
        self.assertEqual(told[-1], ('verifying', total, total))
        stages = [stage for stage, _, _ in told]
        self.assertEqual(stages, sorted(stages))       # all copying, then all verifying

    def test_the_start_note_says_the_percentage(self):
        from ninaivu import __main__ as main
        from ninaivu.server import starting
        steps = []
        with patch.object(starting, 'step', lambda *said: steps.append(said)):
            tell = main._move_progress()
            for done in (0, 10, 50, 50, 100):
                tell('copying', done, 100)
            tell('verifying', 100, 100)
        # The percentage goes with the note as numbers, for the panel's bar.
        self.assertEqual(steps[1], (f'{main.MOVE_STEP}: copying', 10, 100))
        self.assertEqual(len(steps), 5)                 # the repeated 50% is said once
        self.assertEqual(steps[-1], (f'{main.MOVE_STEP}: reading it back to check it', 100, 100))

    def test_the_control_panel_waits_past_its_ceiling_for_a_move(self):
        from ninaivu import __main__ as main
        from ninaivu.desktop import control
        self.assertTrue(main.MOVE_STEP.startswith(control.MOVE_STEP))
        controller = control.Controller.__new__(control.Controller)
        controller.cfg = SimpleNamespace(state_dir=self.source)
        controller.started = None
        controller.on_progress = None
        clock = iter(range(0, 10 ** 6, 600))
        now = __import__('time').time()
        answered = []

        def record():
            answered.append(1)
            return len(answered) > 9      # serving after an hour and a half

        controller.record = record
        controller.capture_running_settings = lambda: None
        controller.save_mode = lambda _mode: None
        controller.mode = None
        controller.runtime = self.base
        # A move that keeps going: its figure moves on at every look (one
        # that sat at the same figure for a quarter of an hour is stuck).
        shares = iter(i / 1000 for i in range(10 ** 6))
        with patch.object(control.starting, 'read', side_effect=lambda *_a, **_k: {
                'since': now, 'step': f'{main.MOVE_STEP}: copying', 'progress': next(shares)}):
            said = controller._wait_for_start(now, clock=lambda: next(clock), sleep=lambda _s: None)
        self.assertEqual(said, 'Ninaivu is running.')


class AfterTheMove(Base):
    def test_every_later_start_takes_models_and_panel_from_the_new_folder(self):
        from ninaivu.desktop.portable import activate_environment
        from ninaivu.server.config import Config
        self.assertEqual(self.move(), self.target)
        launcher = {'NINAIVU_STATE_DIR': str(self.source),
                    'NINAIVU_AI_MODELS_DIR': str(self.source / 'ai-models'),
                    'NINAIVU_HOME': str(self.source / 'control')}
        with patch.dict(os.environ, launcher, clear=False):
            os.environ.pop('NINAIVU_PORTABLE', None)        # a checkout or a service
            activate_environment()
            self.assertEqual(os.environ['NINAIVU_AI_MODELS_DIR'], str(self.target / 'ai-models'))
            self.assertEqual(os.environ['NINAIVU_HOME'], str(self.target / 'control'))
            cfg = Config.load()
            self.assertEqual(cfg.state_dir, self.target)
            self.assertEqual(cfg.db_path, self.target / 'index.db')

    def test_folders_named_elsewhere_are_left_alone(self):
        from ninaivu.desktop.portable import activate_environment
        self.assertEqual(self.move(), self.target)
        elsewhere = str(self.base / 'models-on-another-disk')
        with patch.dict(os.environ, {'NINAIVU_STATE_DIR': str(self.source),
                                     'NINAIVU_AI_MODELS_DIR': elsewhere}):
            activate_environment()
            self.assertEqual(os.environ['NINAIVU_AI_MODELS_DIR'], elsewhere)

    def test_the_stop_command_looks_in_the_new_folder(self):
        from ninaivu.server import stop
        self.assertEqual(self.move(), self.target)
        with patch.dict(os.environ, {'NINAIVU_STATE_DIR': str(self.source)}):
            self.assertEqual(stop.default_state_dir(), self.target)

    def test_settings_carry_the_new_folder(self):
        self.move()
        self.assertEqual(json.loads((self.target / 'config.json').read_text())['state_dir'],
                         str(self.target))


class Status(Base):
    def test_after_a_move_it_says_where_from_and_that_the_old_folder_forwards(self):
        self.move()
        with patch.dict(os.environ, {'NINAIVU_STATE_DIR': str(self.source)}):
            status = relocate.status(self.target)
        self.assertEqual(status['folder'], str(self.target))
        self.assertTrue(status['last']['ok'])
        self.assertEqual(status['last']['source'], str(self.source))
        self.assertEqual(status['forwarded_from'], str(self.source))
        self.assertTrue(status['recovery_present'])
        self.assertIsNotNone(status['free_bytes'])

    def test_a_failure_shows_where_it_tried_and_that_the_copy_is_kept(self):
        with patch.object(relocate, '_read_back', return_value=b'x'):
            self.move()
        last = relocate.status(self.source)['last']
        self.assertFalse(last['ok'])
        self.assertEqual(last['destination'], str(self.target))
        self.assertTrue(last['kept_copy'])
        self.assertIn('at', last)

    def test_a_waiting_move_names_its_folder_and_clears_the_old_result(self):
        relocate.write(self.source / relocate.RESULT, {'ok': False, 'error': 'yesterday'})
        relocate.request(self.source, self.target)
        status = relocate.status(self.source)
        self.assertEqual(status['pending']['destination'], str(self.target))
        self.assertIsNone(status['last'])

    def test_a_failure_copied_in_by_hand_is_not_this_folders(self):
        # Yesterday's failed attempt, copied along with the folder by hand,
        # and the old folder pointed at the copy.
        shutil.copytree(self.source, self.target)
        relocate.write(self.target / relocate.RESULT,
                       {'ok': False, 'error': 'The copied file could not be verified: x'})
        relocate.write(self.source / relocate.REDIRECT, {'destination': str(self.target)})
        with patch.dict(os.environ, {'NINAIVU_STATE_DIR': str(self.source)}):
            status = relocate.status(self.target)
        self.assertIsNone(status['last'])
        self.assertEqual(status['forwarded_from'], str(self.source))

    def test_a_success_recorded_for_another_folder_is_not_shown(self):
        relocate.write(self.source / relocate.RESULT,
                       {'ok': True, 'destination': str(self.base / 'elsewhere')})
        self.assertIsNone(relocate.status(self.source)['last'])

    def test_the_console_reply_names_the_folder_it_will_move_to(self):
        from flask import Flask
        from ninaivu.api import migration_api, server_api
        app = Flask(__name__)
        app.config['MV_CONFIG'] = SimpleNamespace(state_dir=self.source)
        with app.test_request_context(json={'folder': str(self.target)}), \
                patch.object(server_api, '_restart_problem', return_value='Restart manually.'):
            response, status = migration_api.move_index.__wrapped__()
        self.assertEqual(status, 202)
        body = response.get_json()
        self.assertEqual(body['destination'], str(self.target))
        self.assertEqual(body['pending']['files'], 4)


class StartUp(Base):
    def test_a_start_moves_then_opens_the_new_folder(self):
        """The path __main__ takes: move under the start lock, then load."""
        from ninaivu.server.config import Config
        relocate.request(self.source, self.target)
        with patch.dict(os.environ, {'NINAIVU_STATE_DIR': str(self.source)}):
            cfg = Config.load()
            self.assertEqual(cfg.state_dir, self.source)
            with ExitStack() as starts:
                starts.enter_context(runfile.starting_lock(cfg.state_dir))
                moved = relocate.apply_pending(cfg.state_dir, starts=starts)
            self.assertEqual(moved, self.target)
            self.assertEqual(Config.load().state_dir, self.target)



class OtherWriters(Base):
    """Ninaivu's own work runs in the server, which has not started while the
    move runs; anything else writing in the folder is waited for, and what
    changes during the copy is copied again."""

    def _writer(self, name, mode):
        import subprocess
        import sys
        path = self.source / name
        child = subprocess.Popen([sys.executable, '-c',
                                  'import sys, time; f = open(sys.argv[1], sys.argv[2]); '
                                  'print("open", flush=True); time.sleep(60)', str(path), mode],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), 'open')
        return child

    @unittest.skipUnless(Path('/proc/self/fdinfo').is_dir(), 'Linux /proc')
    def test_a_program_writing_in_the_folder_is_seen(self):
        (self.source / 'films').mkdir()
        child = self._writer('films/ffmpeg-output.mp4', 'wb')
        busy = relocate._writers(self.source)
        self.assertEqual(len(busy), 1)
        self.assertIn(f'process {child.pid}', busy[0])
        self.assertIn('films/ffmpeg-output.mp4', busy[0])

    @unittest.skipUnless(Path('/proc/self/fdinfo').is_dir(), 'Linux /proc')
    def test_readers_and_logs_are_not_writers(self):
        self._writer('renditions/323699-v1.jpg', 'rb')
        self._writer('server.log', 'ab')
        self.assertEqual(relocate._writers(self.source), [])

    def test_the_move_waits_for_a_writer_to_finish(self):
        busy = [['ffmpeg (process 42)'], ['ffmpeg (process 42)'], []]
        told = []
        with patch.object(relocate, '_writers', side_effect=lambda _f: busy.pop(0) if busy else []), \
                patch.object(relocate.time, 'sleep'):
            self.assertEqual(self.move(progress=lambda *a: told.append(a)), self.target)
        self.assertEqual(told[0][0], 'waiting')
        self.assertIn('waiting for other programs', self.logged())

    def test_a_writer_that_does_not_finish_leaves_the_index_where_it_is(self):
        with patch.object(relocate, '_writers', return_value=['ffmpeg (process 42, films/x.mp4)']), \
                patch.object(relocate, 'WRITERS_PATIENCE', 0):
            self.assertIsNone(self.move())
        result = self.result(self.source)
        self.assertIn('still writing', result['error'])
        self.assertIn('ffmpeg', result['error'])
        self.assertFalse(self.target.exists())
        self.assertEqual(relocate.resolve(self.source), self.source)

    def test_a_file_changed_during_the_copy_is_copied_again(self):
        real, changed = relocate._hash_copy, []

        def meanwhile(original, copy):
            digest = real(original, copy)
            if original.name == 'index.db' and not changed:
                changed.append(1)
                (self.source / 'index.db').write_bytes(b'written while it was copied' * 50)
            return digest

        with patch.object(relocate, '_hash_copy', meanwhile):
            self.assertEqual(self.move(), self.target)
        self.assertEqual((self.target / 'index.db').read_bytes(), b'written while it was copied' * 50)
        self.assertIn('changed while they were copied', self.logged())
        manifest = json.loads((self.target / relocate.MANIFEST).read_text())
        self.assertEqual(manifest['files']['index.db']['size'], len(b'written while it was copied') * 50)

    def test_a_file_that_keeps_changing_leaves_the_index_where_it_is(self):
        real, n = relocate._hash_copy, [0]

        def always(original, copy):
            digest = real(original, copy)
            if original.name == 'index.db':
                n[0] += 1
                # A different length each time: two writes inside one tick of
                # Windows' clock have the same modified time, and a change of
                # the same length was then not seen (the test failed there
                # now and then; a growing file is what "keeps changing" is).
                (self.source / 'index.db').write_bytes(b'change %d' % n[0] * (50 + n[0]))
            return digest

        with patch.object(relocate, '_hash_copy', always):
            self.assertIsNone(self.move())
        self.assertIn('kept changing', self.result(self.source)['error'])
        self.assertEqual(relocate.resolve(self.source), self.source)

    def test_a_file_changed_by_another_program_is_not_blamed_on_the_disk(self):
        real_back, real_copy, once = relocate._read_back, relocate._hash_copy, []

        def wrong_once(path):
            if path.name == 'face.jpg' and not once:
                once.append(1)
                # Another program rewrites the original just as it is checked.
                (self.source / 'thumbs/ab/face.jpg').write_bytes(b'newer face' * 20)
                return b'garbled'
            return real_back(path)

        with patch.object(relocate, '_read_back', wrong_once), \
                patch.object(relocate, '_hash_copy', real_copy):
            self.assertEqual(self.move(), self.target)
        self.assertEqual((self.target / 'thumbs/ab/face.jpg').read_bytes(), b'newer face' * 20)

    def test_a_log_that_grows_does_not_stop_the_move(self):
        (self.source / 'ninaivu.log').write_text('start\n')
        real, n = relocate._hash_copy, [0]

        def growing(original, copy):
            digest = real(original, copy)
            with (self.source / 'ninaivu.log').open('a') as log:
                n[0] += 1
                log.write(f'line {n[0]}\n')
            return digest

        with patch.object(relocate, '_hash_copy', growing):
            self.assertEqual(self.move(), self.target)


if __name__ == '__main__':
    unittest.main()
