"""Visible startup errors, including when Tk cannot open."""
import subprocess
import sys

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

