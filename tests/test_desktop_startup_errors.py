"""Packaged entry points report failures before claiming the desktop."""
from ninaivu.desktop import app, errors, tray


def test_startup_failure_is_visible(monkeypatch):
    def fail(argv):
        raise ValueError('The relocated index is unavailable')
    told = []
    monkeypatch.setattr(app, '_main', fail)
    monkeypatch.setattr(errors, 'tell', told.append)
    assert app.main([]) == 1
    assert 'relocated index is unavailable' in told[0]


def test_linux_popup_includes_quit(monkeypatch):
    import tkinter
    from types import SimpleNamespace
    entries = []
    class Menu:
        def __init__(self, *args, **kwargs): pass
        def add_separator(self): pass
        def add_command(self, **kwargs): entries.append(kwargs)
        def tk_popup(self, x, y): assert (x, y) == (40, 50)
        def grab_release(self): pass
        def destroy(self): pass
    monkeypatch.setattr(tkinter, 'Menu', Menu)
    calls = []
    host = SimpleNamespace(root=object(), post=lambda action: action(),
                           show_window=lambda: None, start=lambda: None,
                           stop=lambda: None, open_family=lambda: None,
                           quit=lambda: calls.append('quit'))
    tray.linux_popup(host, tray.PanelTray(host), 40, 50)
    next(item for item in entries if item['label'] == 'Quit')['command']()
    assert calls == ['quit']


def test_packaged_entry_reports_import_failure(monkeypatch):
    import sys
    from ninaivu.desktop import launch
    told = []
    monkeypatch.setitem(sys.modules, 'ninaivu.desktop.app', None)
    monkeypatch.setattr(errors, 'tell', told.append)
    assert launch.main([]) == 1
    assert 'Could not open Ninaivu' in told[0]
