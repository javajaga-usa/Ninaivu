"""Ninaivu Control Panel — the double-click entry point.

On Windows, "Start - Ninaivu Control Panel.vbs" runs this with the .venv's
pythonw, so no console window opens; on a Mac, "Ninaivu Control Panel.app"
(built by "Setup Ninaivu.command") runs it with the .venv's python; on Linux,
"Start - Ninaivu Control Panel.sh" does, and so does the menu entry that
`sh launcher/start.sh --add-to-menu` makes.

When the panel cannot open, this says why in a dialog rather than dying
silently, which is all a pythonw process can otherwise do. That includes a
Python with no Tk at all (Homebrew's, until python-tk is installed), so the
message cannot count on Tk to show it.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TITLE = 'Ninaivu Control Panel'


def tell(message):
    """Show *message* with whatever this computer has: Tk, then the
    platform's own dialog, then standard error."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(TITLE, message, parent=root)
        root.destroy()
        return
    except Exception:                                      # noqa: BLE001 — no Tk: try the next way
        pass
    try:
        if sys.platform == 'darwin':
            quoted = message.replace('\\', '\\\\').replace('"', '\\"')
            subprocess.run(['osascript', '-e', f'display dialog "{quoted}" with title "{TITLE}" '
                            'buttons {"OK"} default button "OK" with icon caution'], check=False)
            return
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, TITLE, 0x10)
            return
        if sys.platform.startswith('linux'):
            # Debian and Ubuntu's Python has no Tk until python3-tk is
            # installed, so the desktop's own dialog says it instead.
            import shutil
            for argv in (['zenity', '--error', f'--title={TITLE}', '--no-markup', f'--text={message}'],
                         ['kdialog', '--title', TITLE, '--error', message],
                         ['notify-send', TITLE, message]):
                if shutil.which(argv[0]) and subprocess.run(argv, check=False,
                                                            stderr=subprocess.DEVNULL).returncode == 0:
                    return
    except Exception:                                      # noqa: BLE001 — fall through to stderr
        pass
    print(f'{TITLE}: {message}', file=sys.stderr)


def main():
    try:
        import tkinter  # noqa: F401 — only to find out whether there is a Tk
    except ImportError:
        if sys.platform == 'darwin':
            hint = ('Install python-tk for it (brew install python-tk@3.12), or use the Python '
                    'from python.org, then run Setup Ninaivu again.')
        elif sys.platform.startswith('linux'):
            hint = ('Install Tk for the system\'s Python (on Ubuntu, Debian or Linux Mint: '
                    'sudo apt install python3-tk), then open the Control Panel again.')
        else:
            hint = 'Install Python from python.org, which includes Tk, and run the setup again.'
        tell(f'This Python has no Tk, which the Control Panel is drawn with. {hint}\n\n'
             'The tray (python -m ninaivu.desktop.tray) and the console work without it.')
        return 1
    try:
        from ninaivu.desktop.app import main as panel
        return panel()
    except Exception as error:                             # noqa: BLE001 — shown, not raised
        tell(f'Could not open the Control Panel: {error}\n\n'
             'Install requirements/requirements-desktop.txt into Ninaivu’s environment, '
             'or run the setup again.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
