"""Reopening the desktop window survives UI errors and confirms Tk delivery."""

import queue
import threading
import time

import pytest

from ninaivu.desktop import app, instance


class Window:
    def __init__(self, topmost=False):
        self.visible = False
        self.topmost = topmost
        self.pending = {}
        self.next_timer = 0
        self.raises = 0

    def deiconify(self):
        self.visible = True

    def update_idletasks(self):
        pass

    def attributes(self, name, *value):
        assert name == '-topmost'
        if value:
            self.topmost = value[0]
        return self.topmost

    def after(self, delay, callback):
        self.next_timer += 1
        self.pending[self.next_timer] = (delay, callback)
        return self.next_timer

    def after_cancel(self, timer):
        self.pending.pop(timer)

    def fire(self, delay):
        timer = next(t for t, (d, _) in self.pending.items() if d == delay)
        _, callback = self.pending.pop(timer)
        callback()

    def lift(self):
        self.raises += 1

    def focus_force(self):
        pass

    def winfo_viewable(self):
        return self.visible


def panel(root=None):
    board = app.Dashboard.__new__(app.Dashboard)
    board.root = root if root is not None else Window()
    board.events = queue.Queue()
    board.finished = threading.Event()
    board._refresh_tray = lambda: None
    return board


def test_a_failed_ui_event_does_not_disable_later_window_activation(caplog):
    board = panel()

    def broken():
        raise RuntimeError('failed reading')

    board.post(broken)
    answers = []
    request = threading.Thread(target=lambda: answers.append(board.request_show(timeout=5)))
    request.start()
    # Wait until the listener has actually queued activation, behind the bad event.
    deadline = time.monotonic() + 2
    while board.events.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    try:
        assert board.events.qsize() == 2
        board.pump()
        assert answers == [] and not board.root.visible
        board.root.fire(150)
        request.join(2)
        assert answers == [True] and board.root.visible
        assert 'Control Panel event failed' in caplog.text
    finally:
        board.finished.set()
        request.join(5)


def test_a_failed_tray_refresh_does_not_cancel_the_ui_timer(caplog):
    board = panel()

    def broken():
        raise RuntimeError('tray is unavailable')

    board._refresh_tray = broken
    board.pump()
    assert any(delay == 150 for delay, _ in board.root.pending.values())
    assert 'Control Panel tray refresh failed' in caplog.text


@pytest.mark.parametrize('original', [False, True])
def test_repeated_activation_restores_the_original_window_level(original):
    board = panel(Window(topmost=original))
    assert board.show_window()
    assert board.show_window()
    assert board.root.raises == 2 and board.root.topmost is True
    assert len(board.root.pending) == 1, 'only the newest restoration timer remains'
    board.root.fire(200)
    assert board.root.topmost == original


def test_a_finished_panel_does_not_claim_it_can_be_shown():
    board = panel()
    board.finished.set()
    assert board.show_window() is False
    assert board.root.visible is False


def test_instance_acknowledges_only_after_the_show_callback_finishes(tmp_path):
    first = instance.claim(tmp_path)
    entered, complete = threading.Event(), threading.Event()
    answers = []

    def show():
        entered.set()
        assert complete.wait(5)

    first.serve(show)
    request = threading.Thread(target=lambda: answers.append(instance.tell_to_show(tmp_path)))
    request.start()
    try:
        assert entered.wait(2)
        assert answers == [], 'receiving a request is not confirmation of activation'
        complete.set()
        request.join(2)
        assert answers == [True]
    finally:
        complete.set()
        request.join(5)
        first.close()


def test_failed_activation_is_not_acknowledged_and_the_listener_recovers(tmp_path):
    first = instance.claim(tmp_path)
    attempts = []

    def show():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError('activation callback failed')
        return len(attempts) > 2

    first.serve(show)
    try:
        assert instance.tell_to_show(tmp_path) is False
        assert instance.tell_to_show(tmp_path) is False
        assert instance.claim(tmp_path, patience=0) is None
        assert len(attempts) == 3, 'the same listener handles the successful retry'
    finally:
        first.close()


def test_real_tk_window_reopens_on_the_same_instance_after_a_callback_error(tmp_path):
    tk = pytest.importorskip('tkinter')
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip('no display for Tk')
    root.title('Ninaivu activation regression')
    board = panel(root)
    first = instance.claim(tmp_path)
    first.serve(board.request_show)

    def broken():
        raise RuntimeError('discard this callback, keep the window responsive')

    try:
        for _ in range(3):
            root.withdraw()
            board.post(broken)
            answers = []
            request = threading.Thread(
                target=lambda answers=answers: answers.append(instance.tell_to_show(tmp_path)))
            request.start()
            board.pump()
            deadline = time.monotonic() + 4
            while request.is_alive() and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            request.join(.1)
            assert answers == [True], 'the original desktop instance confirms activation'
            assert root.winfo_viewable() and root.state() == 'normal'
        # Flush the temporary foreground level before destroying the test window.
        deadline = time.monotonic() + .3
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert not root.attributes('-topmost')
    finally:
        board.finished.set()
        first.close()
        root.destroy()
