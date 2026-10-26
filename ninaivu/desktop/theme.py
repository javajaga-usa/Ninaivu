"""The Control Panel's two looks, light and dark, and which one to show.

The colours are the family app's and the console's (``static/css/style.css``)
so the three read as one product: the same warm charcoal ``#141210`` and raised
``#1e1b19`` in the dark, the same cool grey ``#f6f7f9`` and white in the
light, the same blue. A few colours the web pages do not need are added here
(the green of a running server, the log pane's), each with a value for both
looks rather than one colour inverted into the other.

The choice is ``system`` (the default: follow the computer's own light or
dark setting, and change with it), ``light`` or ``dark``. A choice made in the
panel is kept in the Control Panel's settings beside the resource mode.
"""
from __future__ import annotations

import os
import subprocess
import sys

CHOICES = ('system', 'light', 'dark')

#: The colours each look is drawn from; :func:`palette` works out the rest.
BASE = {
    'light': {
        'bg':          '#f6f7f9',
        'surface':     '#ffffff',
        'surface2':    '#f2f4f7',
        'sunken':      '#eceef2',
        'border':      '#dfe3ea',
        'border_soft': '#e9ecf1',
        'text':        '#10151c',
        'text2':       '#5b6675',
        'text3':       '#8b94a3',
        'accent':      '#0b7fd4',
        'accent_fill': '#0a6fba',   # the blue a white label sits on; #0b7fd4 is a shade too light for it
        'accent_fg':   '#ffffff',
        'accent2':     '#6d5efc',
        'success':     '#1f8a4c',
        'success_fill': '#1a7a43',
        'success_fg':  '#ffffff',
        'danger':      '#d8353d',
        'danger_fill': '#c42f37',
        'danger_fg':   '#ffffff',
        'gold':        '#d48806',
        'teal':        '#0e8f99',
        'log_bg':      '#fbfcfd',
        'log_text':    '#2a323d',
        'log_error':   '#c0392b',
        'log_warning': '#9a6700',
        'log_info':    '#0a6fba',
        'log_success': '#17703d',
        'match_bg':    '#fde68a',
        'match_fg':    '#10151c',
    },
    'dark': {
        'bg':          '#141210',
        'surface':     '#1e1b19',
        'surface2':    '#282422',
        'sunken':      '#0e0c0b',
        'border':      '#332e2a',
        'border_soft': '#2a2622',
        'text':        '#efe9e0',
        'text2':       '#b1a79b',
        'text3':       '#968c7f',
        'accent':      '#8fb8ea',
        'accent_fill': '#8fb8ea',
        'accent_fg':   '#0f1a26',
        'accent2':     '#b8aaf2',
        'success':     '#56c184',
        'success_fill': '#1f7a45',   # deeper than the word colours: a white label on a solid button
        'success_fg':  '#ffffff',
        'danger':      '#f2857b',
        'danger_fill': '#c43a41',
        'danger_fg':   '#ffffff',
        'gold':        '#e9bb5e',
        'teal':        '#4fc9d1',
        'log_bg':      '#0e0c0b',
        'log_text':    '#d4ccc1',
        'log_error':   '#ff8a80',
        'log_warning': '#e9bb5e',
        'log_info':    '#8fb8ea',
        'log_success': '#56c184',
        'match_bg':    '#e9bb5e',
        'match_fg':    '#141210',
    },
}


def blend(colour, under, amount):
    """*colour* laid over *under* at *amount* (0 to 1) opacity, as Tk has no
    transparency: the tints are worked out against the surface they sit on."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(under[i:i + 2], 16) for i in (1, 3, 5)]
    return '#' + ''.join(f'{round(x * amount + y * (1 - amount)):02x}' for x, y in zip(a, b))


def palette(name):
    """Every colour the panel uses in the look *name* (``light`` or ``dark``)."""
    p = dict(BASE['dark' if name == 'dark' else 'light'])
    dark = name == 'dark'
    surface, bg = p['surface'], p['bg']
    p['name'] = 'dark' if dark else 'light'
    p['accent_weak'] = blend(p['accent'], surface, .14 if dark else .08)
    p['accent_line'] = blend(p['accent'], surface, .45)
    p['chart_fill'] = blend(p['accent'], surface, .16 if dark else .10)
    # Pressed and hovered: lighter in the dark, deeper in the light.
    toward = '#ffffff' if dark else '#000000'
    for key in ('accent_fill', 'success_fill', 'danger_fill'):
        p[key + '_hover'] = blend(toward, p[key], .07 if dark else .12)
        p[key + '_off'] = blend(p[key], bg, .35)
    p['surface_hover'] = p['surface2'] if not dark else blend('#ffffff', surface, .06)
    # The status pill: its word in the colour (the deeper shade in the light),
    # on a wash of the same colour.
    for state, colour, word in (('running', p['success'], p['success'] if dark else p['success_fill']),
                                ('busy', p['accent'], p['accent'] if dark else p['accent_fill']),
                                ('stopped', p['text2'], p['text2'])):
        p[f'status_{state}_fg'] = word
        p[f'status_{state}_bg'] = blend(colour, bg, .12 if dark else .06) if state != 'stopped' else p['surface2']
        p[f'status_{state}_line'] = blend(colour, bg, .40) if state != 'stopped' else p['border']
    # One small mark of colour per reading and per mode, nothing louder.
    p['card_cpu'] = p['accent']
    p['card_ram'] = p['accent2']
    p['card_power'] = p['gold']
    p['card_server'] = p['success']
    p['card_battery'] = p['teal']
    p['card_disk'] = p['text3']
    for mode, colour in (('standard', p['accent']), ('performance', p['accent2']), ('power-saving', p['success'])):
        p[f'mode_{mode}'] = colour
        p[f'mode_{mode}_tint'] = blend(colour, surface, .12 if dark else .06)
    return p


def resolve(choice, system_dark):
    """The look to show for *choice*, given whether the computer is dark."""
    if choice in ('light', 'dark'):
        return choice
    return 'dark' if system_dark else 'light'


def system_prefers_dark(platform=None, run=subprocess.run, environ=None):
    """Whether the computer is set to dark. Never raises: a computer that will
    not say is taken to be light, which is every system's default."""
    platform = platform or sys.platform
    try:
        if platform == 'darwin':
            # Prints "Dark" when dark; in light the key does not exist at all.
            out = run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                      capture_output=True, text=True, timeout=3)
            return out.stdout.strip().lower() == 'dark'
        if platform == 'win32':
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
    except (OSError, ValueError, subprocess.SubprocessError, ImportError):
        return False
    return _linux_prefers_dark(run, environ=os.environ if environ is None else environ)


def _linux_prefers_dark(run, environ):
    """Linux and the Pi. Each desktop keeps the setting in its own place, and
    a desktop that does not use one leaves it at its default, so any one of
    them saying dark is taken as dark: a theme named in the environment,
    GNOME's switch and theme, Xfce's (MX Linux, Xubuntu), then KDE's colours."""
    if 'dark' in environ.get('GTK_THEME', '').lower():
        return True
    asks = (['gsettings', 'get', 'org.gnome.desktop.interface', 'color-scheme'],
            ['gsettings', 'get', 'org.gnome.desktop.interface', 'gtk-theme'],
            ['xfconf-query', '-c', 'xsettings', '-p', '/Net/ThemeName'])
    for ask in asks:
        try:
            out = run(ask, capture_output=True, text=True, timeout=3)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if 'dark' in (out.stdout or '').lower():
            return True
    config = environ.get('XDG_CONFIG_HOME') or os.path.join(environ.get('HOME') or os.path.expanduser('~'), '.config')
    try:
        with open(os.path.join(config, 'kdeglobals'), encoding='utf-8', errors='replace') as kde:
            for line in kde:
                if line.strip().lower().startswith(('colorscheme=', 'lookandfeelpackage=')):
                    if 'dark' in line.lower():
                        return True
    except OSError:
        pass
    return False


def contrast(fg, bg):
    """The WCAG contrast ratio of two colours, 1 to 21."""
    def luminance(colour):
        channels = []
        for i in (1, 3, 5):
            c = int(colour[i:i + 2], 16) / 255
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        r, g, b = channels
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    lighter, darker = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
