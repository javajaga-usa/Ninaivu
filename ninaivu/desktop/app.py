"""Ninaivu's Control Panel: a window of its own beside the tray.

    python -m ninaivu.desktop.app
    Start - Ninaivu Control Panel.vbs      (Windows, double-click)
    Ninaivu Control Panel.app              (macOS, built by Setup Ninaivu.command)

The tray (:mod:`ninaivu.desktop.tray`) is one small menu; this is the whole
picture on one screen: whether Ninaivu is running and where, Start, Stop and
Restart, the computer's CPU, memory, battery and disk with a minute of CPU
history, the resource mode, and the server's log as it is written, in a
light or a dark look (:mod:`desktop.theme`) that follows the computer's own
unless one is chosen. Most of
that is on the console's Server page too, but the console is only there
while the server is, and this window is for the moments it is not.

The two can run together. Neither holds the server up: both start and stop it
through :class:`desktop.control.Controller`, which never force-kills and
always asks through the server's authenticated stop, and both find a server
that was started another way (``launcher/start.py``, a service, the other).
Closing the window leaves Ninaivu running.

Background work never blocks Tk. Readings and actions run on threads and
come back through a queue that the window empties on its own timer, because
a start can take half a minute and a window that freezes meanwhile looks
like one that has crashed.

It needs Tk, which the python.org installers and the Windows installer bring
(Homebrew's Python needs ``brew install python-tk@3.12``), and ``psutil``,
which ``launcher/start.py`` installs. Without psutil it cannot tell whether
Ninaivu is running, so :func:`main` says so rather than showing a panel that
would call a running server stopped and start a second one.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, messagebox
except ImportError:                                    # pragma: no cover - a Python without Tk
    tk = None
import webbrowser
from pathlib import Path

from . import autostart
from . import theme
from . import control
from .control import UPDATE_ADVICE, Controller, Monitor, read_power
from .logs import LogTail
from ..utils.resources import budget

ICON = Path(__file__).resolve().parents[1] / 'static' / 'icons' / 'icon-192.png'

#: What to install when the panel cannot run, said the same way everywhere.
NEEDS = ('Install what the Control Panel needs into Ninaivu’s environment:\n\n'
         '    pip install -r requirements/requirements-desktop.txt\n\n'
         'or run the setup again (Setup Ninaivu.command on a Mac, start.cmd on Windows).')

# The colours are in :mod:`desktop.theme`, one set for each look.
CARDS = (
    ('cpu', 'Computer CPU'),
    ('ram', 'Computer memory'),
    ('power', 'Battery power draw'),
    ('server', 'Ninaivu process'),
    ('battery', 'Battery'),
    ('disk', 'Free disk space'),
)

THEME_LABELS = (('system', 'System'), ('light', 'Light'), ('dark', 'Dark'))

MODES = (
    ('standard', 'Standard', 'Balanced resources for daily use.'),
    ('performance', 'Performance', 'More threads for intensive work.'),
    ('power-saving', 'Power-saving', 'Fewer threads and efficient scheduling.'),
)


def budget_hint(mode, cpus=None):
    """The one line under a mode: what it gives *this* computer.

    Worked out from :func:`budget` rather than written down, since the
    numbers follow the number of cores; a fixed "4 workers" was wrong on
    most machines it was shown on.
    """
    b = budget(mode, cpus)
    workers = f"{b['workers']} worker{'' if b['workers'] == 1 else 's'}"
    hint = f"{workers} · {b['compute_threads']} AI threads"
    if mode == 'power-saving' and sys.platform == 'win32':
        # Windows' execution-speed throttling (EcoQoS), set by utils.power.
        hint += ' · Throttled'
    return hint


def theme_preference(controller):
    """The look kept in the Control Panel's settings: ``system`` unless
    Light or Dark was chosen."""
    choice = (getattr(controller, 'settings', None) or {}).get('theme', 'system')
    return choice if choice in theme.CHOICES else 'system'


def save_theme_preference(controller, choice):
    if choice not in theme.CHOICES:
        raise ValueError('Unknown look.')
    controller.save_setting('theme', choice)


def small_caps(text):
    """*text* as a spaced, upper-case label, the guide PDFs' small caps. Tk
    has no letter-spacing, so the letters are set a hair space apart."""
    return ' '.join(text.upper())


def font_families(root, platform=None):
    """The interface and code faces for this system: the one each system's
    own windows use, so the panel looks at home on all three."""
    platform = platform or sys.platform
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        available = set()
    if platform == 'darwin':
        ui = tkfont.nametofont('TkDefaultFont').actual('family')   # San Francisco
        mono = 'SF Mono' if 'SF Mono' in available else 'Menlo'
    elif platform == 'win32':
        ui = 'Segoe UI'
        mono = 'Cascadia Mono' if 'Cascadia Mono' in available else 'Consolas'
    else:
        ui = next((f for f in ('Inter', 'Cantarell', 'Ubuntu', 'Noto Sans', 'DejaVu Sans') if f in available),
                  tkfont.nametofont('TkDefaultFont').actual('family'))
        mono = next((f for f in ('DejaVu Sans Mono', 'Liberation Mono', 'Noto Mono') if f in available),
                    tkfont.nametofont('TkFixedFont').actual('family'))
    return ui, mono


class Dashboard:
    def __init__(self, root, controller=None):
        self.root = root
        self.controller = controller or Controller()
        self.monitor = Monitor(self.controller)
        self.events = queue.Queue()
        self.finished = threading.Event()
        self.busy = False
        self.operation_label = ''
        self.cpu_history = []
        self.cards = {}
        self.buttons = []
        self.mode = tk.StringVar(value=self.controller.mode)
        # Each widget's colours by name ('surface', 'text2'...), so a change
        # of look repaints the window in place: nothing is rebuilt, and the
        # readings, the chart's minute and the log keep what they show.
        self.painted = []
        self.status_state = 'stopped'
        self.theme_choice = tk.StringVar(value=theme_preference(self.controller))
        self.following_system = self.theme_choice.get() == 'system'   # for the thread, which may not read Tk's
        self.system_dark = theme.system_prefers_dark()
        self.palette = theme.palette(theme.resolve(self.theme_choice.get(), self.system_dark))

        root.title('Ninaivu Control Panel')

        # Every native widget takes its size from these named fonts; at Tk's
        # defaults they are too small to read on a high-density screen.
        for font_name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
            try:
                f = tkfont.nametofont(font_name)
                if f.cget('size') < 11:
                    f.configure(size=11)
            except tk.TclError:
                pass

        # Nor is Tk's scaling allowed to squash the window below the display's
        # real density.
        try:
            current_scale = float(root.tk.call('tk', 'scaling'))
            if current_scale < 1.3333:
                root.tk.call('tk', 'scaling', 1.3333)
        except (tk.TclError, ValueError):
            pass

        ui, mono = font_families(root)
        self.fonts = {
            'brand':   tkfont.Font(root, family=ui, size=20, weight='bold'),
            'caps':    tkfont.Font(root, family=ui, size=8, weight='bold'),
            'section': tkfont.Font(root, family=ui, size=9, weight='bold'),
            'value':   tkfont.Font(root, family=ui, size=18, weight='bold'),
            'pill':    tkfont.Font(root, family=ui, size=10, weight='bold'),
            'title':   tkfont.Font(root, family=ui, size=11, weight='bold'),
            'body':    tkfont.Font(root, family=ui, size=10),
            'small':   tkfont.Font(root, family=ui, size=9),
            'button':  tkfont.Font(root, family=ui, size=10, weight='bold'),
            'axis':    tkfont.Font(root, family=ui, size=7),
            'mono':    tkfont.Font(root, family=mono, size=11),
        }

        self.style = ttk.Style(root)
        self.style.theme_use('clam')
        self.paint(root, bg='bg')

        command = self.paint(tk.Frame(root, padx=20, pady=10), bg='bg')
        command.pack(fill='x')

        # The dashboard above, the log pane below it when shown; the divider
        # between them can be dragged.
        split = self.paint(tk.PanedWindow(root, orient='vertical', sashwidth=6, borderwidth=0), bg='border_soft')
        split.pack(fill='both', expand=True)
        self.split = split

        upper = self.paint(tk.Frame(split), bg='bg')
        split.add(upper, minsize=350, stretch='always')
        self.upper_pane = upper

        main = self.paint(tk.Frame(upper, padx=20, pady=2), bg='bg')
        main.pack(fill='both', expand=True)

        # The first line: the name, whether it is running, the mode it runs
        # in and the look.
        header = self.paint(tk.Frame(command), bg='bg')
        header.pack(fill='x')

        brand = self.paint(tk.Frame(header), bg='bg')
        brand.pack(side='left', anchor='w')
        self.paint(tk.Label(brand, text='Ninaivu', font=self.fonts['brand']), bg='bg', fg='text').pack(side='left')
        self.paint(tk.Label(brand, text=small_caps('Control panel'), font=self.fonts['caps']),
                   bg='bg', fg='accent2').pack(side='left', padx=(10, 16), pady=(6, 0))

        self.status = tk.StringVar(value='Checking server…')
        self.status_pill = tk.Frame(brand, highlightthickness=1, padx=12, pady=3)
        self.status_pill.pack(side='left', pady=(4, 0))
        self.status_label = tk.Label(self.status_pill, textvariable=self.status, font=self.fonts['pill'])
        self.status_label.pack()

        corner = self.paint(tk.Frame(header), bg='bg')
        corner.pack(side='right', anchor='e')
        # Light, Dark, or whatever the computer is set to (the default).
        switch = self.paint(tk.Frame(corner, highlightthickness=1, padx=2, pady=2),
                            bg='surface2', highlightbackground='border')
        switch.pack(side='right', padx=(12, 0))
        self.theme_buttons = []
        for value, label in THEME_LABELS:
            option = ttk.Radiobutton(switch, text=label, value=value, variable=self.theme_choice,
                                     style='Segment.Toolbutton', command=self.choose_theme, takefocus=True)
            option.pack(side='left')
            self.theme_buttons.append(option)

        self.active_mode = tk.StringVar(value=self.mode_line())
        mode_pill = self.paint(tk.Frame(corner, highlightthickness=1, padx=12, pady=4),
                               bg='surface', highlightbackground='border')
        mode_pill.pack(side='right')
        self.paint(tk.Label(mode_pill, textvariable=self.active_mode, font=self.fonts['small']),
                   bg='surface', fg='text2').pack()

        # The second: what can be done now, and the addresses it answers on.
        action_row = self.paint(tk.Frame(command), bg='bg')
        action_row.pack(fill='x', pady=(10, 0))

        actions = self.paint(tk.Frame(action_row), bg='bg')
        actions.pack(side='left', anchor='w')
        self.start_button = self._action_button(actions, '▶  Start', lambda: self.run(self.controller.start, 'Starting'), 'Start.TButton')
        self.stop_button = self._action_button(actions, '■  Stop', lambda: self.run(self.controller.stop, 'Stopping'), 'Stop.TButton')
        self.restart_button = self._action_button(actions, '↻  Restart', lambda: self.run(self.restart, 'Restarting'), 'TButton')
        self.paint(tk.Frame(actions, width=1), bg='border').pack(side='left', fill='y', padx=(4, 12), pady=4)
        self.button(actions, 'Open the family app', lambda: webbrowser.open(self.controller.server_url()))
        self.button(actions, 'Open the console', lambda: webbrowser.open(self.controller.server_url(True)))

        self.endpoints_info = tk.StringVar(value=self.format_endpoints())
        self.endpoints_label = self.paint(
            tk.Label(action_row, textvariable=self.endpoints_info, font=self.fonts['small'], justify='right', anchor='e'),
            bg='bg', fg='text2')
        self.endpoints_label.pack(side='right', fill='x', expand=True, anchor='e', padx=(8, 0))
        # On one line, the addresses of a running server are wider than the rest
        # of the panel together, and the first window was sized to them: nearly
        # the whole screen wide. They wrap to the room the buttons leave instead.
        self.endpoints_label.configure(wraplength=self._scaled(420))
        self.endpoints_label.bind('<Configure>', lambda e: self.endpoints_label.configure(
            wraplength=max(self._scaled(240), e.width)))

        # A hairline that moves only while something is starting or stopping.
        # (Tk's own progress bar is 18 pixels tall whatever it is told, so it
        # is shown through a slot of three.)
        slot = self.paint(tk.Frame(command, height=3), bg='bg')
        slot.pack(fill='x', pady=(10, 0))
        slot.pack_propagate(False)
        self.operation_progress = ttk.Progressbar(slot, mode='indeterminate', style='Operation.Horizontal.TProgressbar')
        self.operation_progress.pack(fill='both', expand=True)

        # -- this computer ----------------------------------------------------
        self.section(main, 'This computer')

        grid = self.paint(tk.Frame(main), bg='bg')
        grid.pack(fill='x')
        grid.columnconfigure((0, 1, 2), weight=1, uniform='card')

        self.card_bars = {}
        for index, (key, title) in enumerate(CARDS):
            frame = self.paint(tk.Frame(grid, padx=14, pady=8, highlightthickness=1),
                               bg='surface', highlightbackground='border')
            frame.grid(row=index // 3, column=index % 3, sticky='nsew',
                       padx=(0, 10) if index % 3 != 2 else 0, pady=(0, 8))

            label_row = self.paint(tk.Frame(frame), bg='surface')
            label_row.pack(fill='x')
            # The reading's one mark of colour, a small square before its name.
            self.paint(tk.Frame(label_row, width=7, height=7), bg=f'card_{key}').pack(side='left', padx=(0, 7))
            self.paint(tk.Label(label_row, text=small_caps(title), font=self.fonts['caps']),
                       bg='surface', fg='text2').pack(side='left')
            value = tk.StringVar(value='—')
            detail = tk.StringVar(value='Waiting for readings')
            self.paint(tk.Label(frame, textvariable=value, font=self.fonts['value']),
                       bg='surface', fg='text').pack(anchor='w', pady=(2, 1))

            if key in ('cpu', 'ram'):
                bar = self.paint(tk.Canvas(frame, height=4, highlightthickness=0), bg='surface')
                bar.pack(fill='x', pady=(2, 4))
                self.card_bars[key] = bar

            self.paint(tk.Label(frame, textvariable=detail, font=self.fonts['small'], wraplength=260, justify='left'),
                       bg='surface', fg='text2').pack(anchor='w')
            self.cards[key] = (value, detail)

        # -- a minute of CPU ------------------------------------------------------
        self.chart_title = tk.StringVar(value='Last 60 samples')
        self.section(main, 'CPU activity', aside=self.chart_title)

        chart_container = self.paint(tk.Frame(main, highlightthickness=1, padx=8, pady=6),
                                     bg='surface', highlightbackground='border')
        chart_container.pack(fill='x', pady=(0, 2))
        self.chart = self.paint(tk.Canvas(chart_container, height=64, highlightthickness=0), bg='surface')
        self.chart.pack(fill='x')
        self.chart.bind('<Configure>', lambda _e: self._draw_chart())

        # -- the resource mode ------------------------------------------------
        self.section(main, 'Resource mode')
        profiles = self.paint(tk.Frame(main), bg='bg')
        profiles.pack(fill='x')
        profiles.columnconfigure((0, 1, 2), weight=1, uniform='mode')
        self.radios = []
        self.mode_cards = {}
        for i, (mode, title, description) in enumerate(MODES):
            card = tk.Frame(profiles, padx=14, pady=8)
            card.grid(row=0, column=i, sticky='nsew', padx=(0, 10) if i < 2 else 0)

            radio = ttk.Radiobutton(card, text=title, value=mode, variable=self.mode, command=self._update_mode_selection)
            radio.pack(anchor='w')
            self.radios.append(radio)
            words = tk.Label(card, text=description, font=self.fonts['small'], wraplength=250, justify='left')
            words.pack(anchor='w', padx=(24, 0), pady=(1, 5))
            hint = tk.Label(card, text=budget_hint(mode), font=self.fonts['caps'], padx=7, pady=2,
                            highlightthickness=1)
            hint.pack(anchor='w', padx=(24, 0))
            self.mode_cards[mode] = (card, radio, words, hint)

        self.mode_description = tk.StringVar()
        self.paint(tk.Label(main, textvariable=self.mode_description, font=self.fonts['small'], wraplength=950, justify='left'),
                   bg='bg', fg='text2').pack(anchor='w', pady=(6, 0))

        mode_actions = self.paint(tk.Frame(main), bg='bg')
        mode_actions.pack(fill='x', pady=(8, 0))
        self.logs_visible = False
        self.log_button_text = tk.StringVar(value='View logs')
        self.button(mode_actions, 'Apply mode', self.apply_selected_mode, True)
        self.log_button = self.button(mode_actions, 'View logs', self.toggle_logs, textvariable=self.log_button_text)
        self.button(mode_actions, 'Trust the HTTPS certificate', self.open_certificate)
        if autostart.supported():
            # So a restart for an update, or after a power cut, brings the
            # family app back without anybody opening Ninaivu.
            self.start_at_sign_in = tk.BooleanVar(value=autostart.enabled())
            ttk.Checkbutton(mode_actions, text='Start Ninaivu when I sign in',
                            variable=self.start_at_sign_in,
                            command=self.toggle_start_at_sign_in).pack(side='left', padx=(8, 0))

        self.notice = tk.StringVar(value='Applying a mode gracefully restarts a running server. '
                                         'Closing this panel leaves Ninaivu running.')
        self.paint(tk.Label(main, textvariable=self.notice, font=self.fonts['small'], wraplength=950, justify='left'),
                   bg='bg', fg='text3').pack(anchor='w', pady=(8, 0))
        # Always shown: the version, and that an update starts with Stop.
        from .. import __version__
        self.paint(tk.Label(main, text=UPDATE_ADVICE.format(version=__version__), font=self.fonts['small'],
                            wraplength=950, justify='left'),
                   bg='bg', fg='text3').pack(anchor='w', pady=(2, 10))

        self.build_logs(split)
        self.apply_theme()
        self._fit_window()
        root.protocol('WM_DELETE_WINDOW', self.close)
        threading.Thread(target=self.monitor_loop, daemon=True, name='ninaivu-panel-monitor').start()
        threading.Thread(target=self.power_loop, daemon=True, name='ninaivu-panel-power').start()
        threading.Thread(target=self.appearance_loop, daemon=True, name='ninaivu-panel-appearance').start()
        root.after(100, self.pump)
        root.after(200, self.poll_logs)

    # -- the two looks ------------------------------------------------------------

    def paint(self, widget, **colours):
        """Colour *widget* from the palette by name (``bg='surface'``) and
        remember it, so :meth:`apply_theme` can colour it again."""
        self.painted.append((widget, colours))
        self._colour(widget, colours)
        return widget

    def _colour(self, widget, colours):
        try:
            widget.configure(**{option: self.palette[name] for option, name in colours.items()})
        except tk.TclError:
            pass

    def section(self, parent, title, aside=None):
        """A section's heading: its name in small caps, a hairline after it,
        and on the right a muted note (*aside*, a StringVar) when there is one."""
        row = self.paint(tk.Frame(parent), bg='bg')
        row.pack(fill='x', pady=(8, 6))
        self.paint(tk.Label(row, text=small_caps(title), font=self.fonts['section']),
                   bg='bg', fg='text2').pack(side='left')
        if aside is not None:
            self.paint(tk.Label(row, textvariable=aside, font=self.fonts['small']),
                       bg='bg', fg='text3').pack(side='right', padx=(10, 0))
        self.paint(tk.Frame(row, height=1), bg='border').pack(side='left', fill='x', expand=True, padx=(12, 0), pady=(1, 0))
        return row

    def choose_theme(self):
        """The switch was pressed: show that look, and keep the choice."""
        choice = self.theme_choice.get()
        self.following_system = choice == 'system'
        if self.following_system:
            self.system_dark = theme.system_prefers_dark()
        try:
            save_theme_preference(self.controller, choice)
        except OSError:
            pass                                           # shown now, just not remembered
        self.apply_theme()

    def apply_theme(self):
        """Colour everything for the chosen look, in place."""
        p = self.palette = theme.palette(theme.resolve(self.theme_choice.get(), self.system_dark))
        self._style_widgets(p)
        for widget, colours in self.painted:
            self._colour(widget, colours)
        self.show_status(self.status_state)
        self._update_mode_selection()
        self._paint_logs(p)
        self.operation_progress.configure(style='Busy.Horizontal.TProgressbar' if self.busy
                                          else 'Operation.Horizontal.TProgressbar')
        for bar in self.card_bars.values():
            self._update_bar(bar, getattr(bar, 'reading', 0), 100)
        self._draw_chart()
        self._title_bar(p['name'] == 'dark')

    def _style_widgets(self, p):
        """The ttk widgets' styles: buttons, the mode radios, the switch, the
        log pane's controls. Changing a style changes every widget using it."""
        style, f = self.style, self.fonts
        flat = dict(relief='solid', borderwidth=1, focusthickness=1, focuscolor=p['accent'])

        def solid(name, fill, fg):
            style.configure(name, font=f['button'], padding=(14, 5), background=p[fill], foreground=p[fg],
                            bordercolor=p[fill], lightcolor=p[fill], darkcolor=p[fill], **flat)
            style.map(name,
                      background=[('disabled', p[fill + '_off']), ('pressed', p[fill + '_hover']), ('active', p[fill + '_hover'])],
                      bordercolor=[('disabled', p[fill + '_off']), ('active', p[fill + '_hover'])],
                      lightcolor=[('disabled', p[fill + '_off']), ('active', p[fill + '_hover'])],
                      darkcolor=[('disabled', p[fill + '_off']), ('active', p[fill + '_hover'])],
                      foreground=[('disabled', p['text3'] if p['name'] == 'dark' else '#ffffff')])

        style.configure('TButton', font=f['button'], padding=(14, 5), background=p['surface'], foreground=p['text'],
                        bordercolor=p['border'], lightcolor=p['surface'], darkcolor=p['surface'], **flat)
        style.map('TButton',
                  background=[('disabled', p['surface2']), ('pressed', p['surface_hover']), ('active', p['surface_hover'])],
                  lightcolor=[('disabled', p['surface2']), ('active', p['surface_hover'])],
                  darkcolor=[('disabled', p['surface2']), ('active', p['surface_hover'])],
                  bordercolor=[('focus', p['accent_line']), ('active', p['accent_line'])],
                  foreground=[('disabled', p['text3'])])
        solid('Accent.TButton', 'accent_fill', 'accent_fg')
        solid('Start.TButton', 'success_fill', 'success_fg')
        solid('Stop.TButton', 'danger_fill', 'danger_fg')

        # The look switch: three words in a tray, the chosen one raised.
        style.configure('Segment.Toolbutton', font=f['small'], padding=(10, 2), background=p['surface2'],
                        foreground=p['text2'], bordercolor=p['surface2'], lightcolor=p['surface2'],
                        darkcolor=p['surface2'], relief='flat', focuscolor=p['accent'])
        style.map('Segment.Toolbutton',
                  background=[('selected', p['surface']), ('active', p['surface_hover'])],
                  foreground=[('selected', p['text']), ('active', p['text'])],
                  bordercolor=[('selected', p['border']), ('active', p['surface_hover'])],
                  lightcolor=[('selected', p['surface']), ('active', p['surface_hover'])],
                  darkcolor=[('selected', p['surface']), ('active', p['surface_hover'])],
                  relief=[('selected', 'flat')])

        indicator = dict(indicatorbackground=p['surface'], indicatorforeground=p['accent'], indicatordiameter=14,
                         indicatormargin=(0, 0, 8, 0),
                         upperbordercolor=p['text3'], lowerbordercolor=p['text3'], focuscolor=p['accent'])
        indicator_map = dict(indicatorbackground=[('pressed', p['surface2']), ('selected', p['surface'])],
                             upperbordercolor=[('selected', p['accent']), ('active', p['accent_line'])],
                             lowerbordercolor=[('selected', p['accent']), ('active', p['accent_line'])])
        style.configure('TRadiobutton', font=f['title'], padding=(0, 2), background=p['surface'], foreground=p['text'],
                        **indicator)
        style.map('TRadiobutton', background=[('active', p['surface'])], foreground=[('disabled', p['text3'])],
                  **indicator_map)
        for mode, _title, _words in MODES:
            name = self._mode_style(mode)
            tint = p[f'mode_{mode}_tint']
            style.configure(name, font=f['title'], padding=(0, 2), background=tint, foreground=p['text'], **indicator)
            style.map(name, background=[('active', tint)], foreground=[('disabled', p['text3'])], **indicator_map)
        for name, bg, fg in (('TCheckbutton', 'bg', 'text'), ('Log.TCheckbutton', 'log_bg', 'log_text')):
            style.configure(name, font=f['body'], background=p[bg], foreground=p[fg], **indicator)
            style.map(name, background=[('active', p[bg])], foreground=[('disabled', p['text3'])], **indicator_map)

        style.configure('Operation.Horizontal.TProgressbar', thickness=2, troughcolor=p['bg'], background=p['bg'],
                        bordercolor=p['bg'], lightcolor=p['bg'], darkcolor=p['bg'])
        style.configure('Busy.Horizontal.TProgressbar', thickness=2, troughcolor=p['border_soft'],
                        background=p['accent'], bordercolor=p['border_soft'], lightcolor=p['accent'],
                        darkcolor=p['accent'])

        field = dict(fieldbackground=p['surface'], foreground=p['text'], bordercolor=p['border'],
                     lightcolor=p['surface'], darkcolor=p['surface'], insertcolor=p['text'],
                     selectbackground=p['accent_weak'], selectforeground=p['text'])
        style.configure('TEntry', padding=(6, 3), **field)
        style.map('TEntry', bordercolor=[('focus', p['accent'])], lightcolor=[('focus', p['accent_weak'])])
        style.configure('TCombobox', padding=(6, 3), background=p['surface'], arrowcolor=p['text2'], **field)
        style.map('TCombobox', fieldbackground=[('readonly', p['surface'])], foreground=[('readonly', p['text'])],
                  background=[('active', p['surface_hover']), ('readonly', p['surface'])],
                  bordercolor=[('focus', p['accent'])], selectbackground=[('readonly', p['surface'])],
                  selectforeground=[('readonly', p['text'])])
        # The list a combobox drops down is a plain Tk listbox.
        for option, name in (('background', 'surface'), ('foreground', 'text'),
                             ('selectBackground', 'accent_weak'), ('selectForeground', 'text')):
            self.root.option_add(f'*TCombobox*Listbox.{option}', p[name])
        style.configure('Vertical.TScrollbar', background=p['surface2'], troughcolor=p['log_bg'],
                        bordercolor=p['log_bg'], lightcolor=p['surface2'], darkcolor=p['surface2'],
                        arrowcolor=p['text3'], relief='flat')
        style.map('Vertical.TScrollbar', background=[('active', p['border'])])

    @staticmethod
    def _mode_style(mode):
        return 'Mode' + mode.title().replace('-', '') + '.TRadiobutton'

    def _title_bar(self, dark):
        """The window's own frame in the same look, where the system lets a
        program say: a Mac's title bar, and Windows 10 and 11's."""
        root = self.root
        try:
            if sys.platform == 'darwin':
                appearance = {'system': 'auto'}.get(self.theme_choice.get(), 'darkaqua' if dark else 'aqua')
                root.tk.call('::tk::unsupported::MacWindowStyle', 'appearance', root._w, appearance)
            elif os.name == 'nt':
                import ctypes
                root.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
                value = ctypes.c_int(1 if dark else 0)
                for attribute in (20, 19):                 # DWMWA_USE_IMMERSIVE_DARK_MODE, and its pre-2004 number
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                                  ctypes.sizeof(value)) == 0:
                        break
        except (tk.TclError, AttributeError, OSError):
            pass

    def appearance_loop(self):
        """Follows the computer from light to dark and back while the panel
        is set to System. Off Tk's thread: asking can mean running a command."""
        while not self.finished.wait(4):
            if self.following_system:
                dark = theme.system_prefers_dark()
                if dark != self.system_dark:
                    self.events.put(('appearance', dark))


    def _scaled(self, pixels):
        """*pixels* at 100%, at the display's scaling (the panel is DPI-aware)."""
        try:
            scale = max(1.0, float(self.root.tk.call('tk', 'scaling')) / 1.3333)
        except (tk.TclError, ValueError):
            scale = 1.0
        return int(pixels * scale)

    def _room(self):
        """The most of the screen the window may take: the taskbar and the
        title bar have the rest."""
        root = self.root
        return int(root.winfo_screenwidth() * 0.95), int(root.winfo_screenheight() * 0.88)

    def _fit_window(self):
        """Open at the size the panel's contents ask for.

        Fonts and padding grow with the display's scaling, so a size fixed in
        pixels left the right-hand cards cut off at 150%. Measured once
        everything is built, and kept on the screen: a screen too small for
        all of it gets the whole screen.
        """
        root = self.root
        root.update_idletasks()
        room_w, room_h = self._room()
        width = min(root.winfo_reqwidth(), room_w, self._scaled(1300))
        height = min(root.winfo_reqheight(), room_h)
        root.minsize(min(self._scaled(820), width), min(self._scaled(620), height))
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 3)
        root.geometry(f"{width}x{height}+{x}+{y}")
        self._fitted = (width, height)
        self._grown_for_readings = False

    def _grow_to_fit(self, force=False):
        """Make the window taller (never smaller) when its contents grew: the
        first readings and a running server's addresses take more lines than
        the placeholders it was measured with, and the log pane adds its own
        height. Without this the bottom row - the buttons and the notice - was
        cut off on the first run. A size the person chose is left alone,
        unless *force* (they asked for the logs)."""
        root = self.root
        if not root.winfo_viewable():
            # Not on the screen yet, so it has no size to compare: once it is.
            root.after(100, lambda: self._grow_to_fit(force))
            return
        root.update_idletasks()
        current = (root.winfo_width(), root.winfo_height())
        if not force and current != getattr(self, '_fitted', current):
            return
        room_w, room_h = self._room()
        width = max(current[0], min(root.winfo_reqwidth(), room_w, self._scaled(1300)))
        height = max(current[1], min(root.winfo_reqheight(), room_h))
        if (width, height) != current:
            x = min(max(0, root.winfo_x()), max(0, root.winfo_screenwidth() - width))
            y = min(max(0, root.winfo_y()), max(0, root.winfo_screenheight() - height - 48))
            root.geometry(f"{width}x{height}+{x}+{y}")
            root.update_idletasks()
        self._fitted = (root.winfo_width(), root.winfo_height())
        if force and self.logs_visible:
            # The height the logs were opened at: hiding them again goes back
            # to the height before, unless the person resized it meanwhile.
            self._height_with_logs = self._fitted[1]

    def _shrink_after_logs(self, before, with_logs, pane_height):
        """Give back the height the log pane took. Untouched since the logs
        opened, the window returns to the height it had before them; resized
        while they were open, it loses the pane's height instead, but never
        below what the dashboard needs to show its bottom row (or the room the
        screen has), nor below the window's minimum, nor taller than it is
        now; the width and the place on the screen stay."""
        root = self.root
        if not root.winfo_viewable() or root.wm_state() == 'zoomed':
            return
        root.update_idletasks()
        width, current = root.winfo_width(), root.winfo_height()
        if before and current == with_logs:
            height = before
        else:
            # The same height _grow_to_fit would give the dashboard: lower,
            # and its first reading would make the window taller again.
            needed = min(root.winfo_reqheight(), self._room()[1])
            height = max(needed, current - pane_height)
        height = min(current, max(root.minsize()[1], height))
        if height != current:
            root.geometry(f"{width}x{height}+{root.winfo_x()}+{root.winfo_y()}")
            root.update_idletasks()
        self._fitted = (root.winfo_width(), root.winfo_height())

    def mode_line(self):
        return 'Active mode: ' + self.controller.mode.replace('-', ' ').title()

    def format_endpoints(self):
        record = self.controller.record()
        if record:
            scheme = (record.get('scheme') or 'http').upper()
            port = record.get('port', 443 if scheme == 'HTTPS' else 80)
            pid = record.get('pid', '—')
            return (f"Family app: {self.controller.server_url()}  ·  Console: {self.controller.server_url(True)}"
                    f"  ·  {scheme} :{port} (PID {pid})")
        target = self.controller.server_url()
        state_dir = getattr(getattr(self.controller, 'cfg', None), 'state_dir', None)
        state = f"  ·  State: {Path(state_dir).name}" if state_dir else ''
        return f"Stopped  ·  Starts at: {target}{state}"

    # -- the log pane ---------------------------------------------------------

    def log_path(self):
        """The file the log pane follows: the server's, or the local AI's
        (Ollama, when the panel started it)."""
        name = 'server.log' if self.log_source.get() == 'Server' else 'ollama.log'
        return self.controller.runtime / name

    def build_logs(self, split):
        panel = self.paint(tk.Frame(split, padx=20, pady=12), bg='log_bg')
        self.log_panel = panel

        toolbar = self.paint(tk.Frame(panel), bg='log_bg')
        toolbar.pack(fill='x', pady=(0, 10))

        left = self.paint(tk.Frame(toolbar), bg='log_bg')
        left.pack(side='left')
        self.paint(tk.Label(left, text='●', font=self.fonts['small']), bg='log_bg', fg='success').pack(side='left', padx=(0, 6))
        self.paint(tk.Label(left, text=small_caps('Live logs'), font=self.fonts['section']),
                   bg='log_bg', fg='log_text').pack(side='left', padx=(0, 14))

        self.log_source = tk.StringVar(value='Server')
        source = ttk.Combobox(left, textvariable=self.log_source, values=['Server', 'Local AI'], state='readonly',
                              width=10, font=self.fonts['body'])
        source.pack(side='left', padx=(0, 8))
        source.bind('<<ComboboxSelected>>', lambda _: self.switch_log())
        self.log_source_box = source

        self.paint(tk.Frame(left, width=1), bg='border').pack(side='left', fill='y', padx=8, pady=4)

        self.log_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text='Auto-scroll', variable=self.log_follow, style='Log.TCheckbutton').pack(side='left', padx=5)

        self.log_paused = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text='Pause', variable=self.log_paused, style='Log.TCheckbutton').pack(side='left', padx=5)

        right = self.paint(tk.Frame(toolbar), bg='log_bg')
        right.pack(side='right')

        ttk.Button(right, text='✕ Hide', command=self.hide_logs).pack(side='right', padx=(6, 0))
        ttk.Button(right, text='Clear view', command=self.clear_log_view).pack(side='right', padx=3)
        ttk.Button(right, text='Copy view', command=self.copy_logs).pack(side='right', padx=3)
        self.paint(tk.Frame(right, width=1), bg='border').pack(side='right', fill='y', padx=8, pady=4)
        ttk.Button(right, text='Find next', command=self.find_log).pack(side='right', padx=3)
        self.log_query = tk.StringVar()
        search = ttk.Entry(right, textvariable=self.log_query, font=self.fonts['body'], width=16)
        search.pack(side='right', padx=(4, 3))
        search.bind('<Return>', lambda _: self.find_log())
        self.paint(tk.Label(right, text='Find', font=self.fonts['small']), bg='log_bg', fg='text2').pack(side='right', padx=(0, 4))

        holder = self.paint(tk.Frame(panel, highlightthickness=1), bg='log_bg', highlightbackground='border')
        holder.pack(fill='both', expand=True)

        self.log_text = tk.Text(holder, font=self.fonts['mono'], wrap='word', height=9, borderwidth=0,
                                highlightthickness=0, state='disabled', padx=12, pady=10, spacing1=2, spacing3=2)
        self.paint(self.log_text, bg='log_bg', fg='log_text', insertbackground='log_text',
                   selectbackground='accent_weak', selectforeground='text')
        scroll = ttk.Scrollbar(holder, command=self.log_text.yview)
        scroll.pack(side='right', fill='y')
        self.log_text.pack(side='left', fill='both', expand=True)
        self.log_text.configure(yscrollcommand=scroll.set)

        self.log_info = tk.StringVar(value='Waiting for server output. Log files stay on this computer.')
        self.paint(tk.Label(panel, textvariable=self.log_info, font=self.fonts['small']),
                   bg='log_bg', fg='text2').pack(anchor='w', pady=(8, 0))
        self.log_tail = LogTail(self.log_path())

    def _paint_logs(self, p):
        """The log's colours: errors red, warnings amber, addresses blue."""
        for tag in ('error', 'warning', 'info', 'success'):
            self.log_text.tag_configure(tag, foreground=p['log_' + tag])
        self.log_text.tag_configure('match', background=p['match_bg'], foreground=p['match_fg'])
        try:
            # The open list of an already-made combobox is not reached by option_add.
            popdown = self.root.tk.call('ttk::combobox::PopdownWindow', self.log_source_box)
            self.root.tk.call(f'{popdown}.f.l', 'configure', '-background', p['surface'], '-foreground', p['text'],
                              '-selectbackground', p['accent_weak'], '-selectforeground', p['text'])
        except tk.TclError:
            pass

    def clear_log_view(self):
        """Empties the pane. The log file itself is never touched."""
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def find_log(self):
        query = self.log_query.get()
        previous = self.log_text.tag_ranges('match')
        start = str(previous[-1]) if previous else '1.0'
        self.log_text.tag_remove('match', '1.0', 'end')
        if not query:
            return
        index = self.log_text.search(query, start, stopindex='end', nocase=True)
        if not index:
            index = self.log_text.search(query, '1.0', stopindex=start, nocase=True)
        if index:
            self.log_text.tag_add('match', index, f'{index}+{len(query)}c')
            self.log_follow.set(False)
            self.log_text.see(index)
            self.log_info.set('Match found · Auto-scroll paused so you can read. Tick Auto-scroll to follow live output.')
        else:
            self.log_info.set('No match in the lines shown.')

    def copy_logs(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get('1.0', 'end-1c'))
        self.log_info.set('Copied the lines shown. Look for private paths and names before sharing them.')

    def switch_log(self):
        self.log_tail = LogTail(self.log_path())
        self.clear_log_view()
        self.poll_logs(again=False)

    def _update_log_display(self):
        try:
            text = self.log_tail.read()
            if text:
                self.log_text.configure(state='normal')
                for line in text.splitlines(keepends=True):
                    self.log_text.insert('end', line, line_tag(line))
                # At most 2,000 lines and 200 KB on screen, however long it runs.
                excess = int(self.log_text.index('end-1c').split('.')[0]) - 2000
                if excess > 0:
                    self.log_text.delete('1.0', f'{excess+1}.0')
                char_count = self.log_text.count('1.0', 'end-1c', 'chars')
                chars = char_count[0] if char_count else 0
                if chars > 200000:
                    self.log_text.delete('1.0', f'1.0 + {chars-200000} chars')
                self.log_text.configure(state='disabled')
                if self.log_follow.get():
                    self.log_text.see('end')
                self.log_info.set(f"{self.log_source.get()} · Live output · Showing at most 2,000 lines / 200 KB")
            elif not self.log_tail.path.exists():
                if self.log_source.get() == 'Server':
                    self.log_info.set('No log yet. A server started from this panel or the tray writes one here.')
                else:
                    self.log_info.set('No local AI log. It appears when the panel starts Ollama with Ninaivu.')
        except OSError as error:
            self.log_info.set(f'Log temporarily unavailable: {error.strerror or "read error"}')

    def poll_logs(self, again=True):
        if self.finished.is_set():
            return
        if not self.log_paused.get():
            self._update_log_display()
        if again:
            self.root.after(750, self.poll_logs)

    # -- buttons and their state ------------------------------------------------

    def button(self, parent, text, command, accent=False, textvariable=None):
        kwargs = {'style': 'Accent.TButton' if accent else 'TButton'}
        if textvariable is not None:
            kwargs['textvariable'] = textvariable
        else:
            kwargs['text'] = text
        button = ttk.Button(parent, command=command, **kwargs)
        button.pack(side='left', padx=(0, 8))
        self.buttons.append(button)
        return button

    def _action_button(self, parent, text, command, style):
        button = ttk.Button(parent, text=text, command=command, style=style)
        button.pack(side='left', padx=(0, 8))
        self.buttons.append(button)
        return button

    def _update_mode_selection(self):
        """Marks the chosen mode's card in its colour and says what it gives."""
        selected = self.mode.get()
        p = self.palette
        for mode, (card, radio, words, hint) in self.mode_cards.items():
            chosen = mode == selected
            fill = p[f'mode_{mode}_tint'] if chosen else p['surface']
            card.configure(bg=fill, highlightbackground=p[f'mode_{mode}'] if chosen else p['border'],
                           highlightcolor=p[f'mode_{mode}'], highlightthickness=2 if chosen else 1,
                           # The 1px a plain card's border leaves, so choosing one does not move its words.
                           padx=13 if chosen else 14, pady=7 if chosen else 8)
            radio.configure(style=self._mode_style(mode) if chosen else 'TRadiobutton')
            words.configure(bg=fill, fg=p['text2'])
            hint.configure(bg=p['surface'] if chosen else p['surface2'], fg=p[f'mode_{mode}'] if chosen else p['text2'],
                           highlightbackground=p['border'] if not chosen else p[f'mode_{mode}'],
                           highlightthickness=1)
        self.describe_mode()

    def describe_mode(self):
        b = budget(self.mode.get())
        self.mode_description.set(f"Selected: {b['workers']} scan workers · {b['compute_threads']} AI/video threads · "
                                  f"{b['server_threads']} request threads. Image resolution and quality stay unchanged.")

    def restart(self):
        self.controller.stop()
        return self.controller.start()

    def apply_selected_mode(self):
        selected = self.mode.get()
        self.run(lambda: self.controller.apply_mode(selected))

    def run(self, operation, label='Applying settings'):
        """*operation* on a worker thread; its one-line answer comes back
        through the queue. One at a time: the buttons are greyed meanwhile."""
        if self.busy:
            return
        self.busy = True
        self.operation_label = label
        self.status.set(label + '…')
        self.notice.set(label + '… Ninaivu finishes the files it is working on before it stops.')
        self.show_status('busy')
        self.set_controls()
        self.operation_progress.configure(style='Busy.Horizontal.TProgressbar')
        self.operation_progress.start(12)

        def worker():
            try:
                self.events.put(('done', operation()))
            except Exception as error:                     # noqa: BLE001 — shown, not raised
                self.events.put(('error', str(error) or error.__class__.__name__))

        threading.Thread(target=worker, daemon=True, name='ninaivu-panel-action').start()

    def set_controls(self, running=None):
        for widget in [*self.buttons, *self.radios]:
            widget.configure(state='disabled' if self.busy else 'normal')
        if not self.busy and running is not None:
            self.start_button.configure(state='disabled' if running else 'normal')
            self.stop_button.configure(state='normal' if running else 'disabled')
            self.restart_button.configure(state='normal' if running else 'disabled')

    def show_status(self, running):
        """The pill beside the name: *running* is True, False or ``'busy'``."""
        state = running if running in ('running', 'stopped', 'busy') else 'running' if running else 'stopped'
        self.status_state = state
        p = self.palette
        self.status_pill.configure(bg=p[f'status_{state}_bg'], highlightbackground=p[f'status_{state}_line'])
        self.status_label.configure(bg=p[f'status_{state}_bg'], fg=p[f'status_{state}_fg'])
        if state == 'running':
            self.status.set('●  Running')
        elif state == 'stopped':
            self.status.set('○  Stopped')

    # -- readings -----------------------------------------------------------------

    def monitor_loop(self):
        while not self.finished.is_set():
            try:
                self.events.put(('sample', self.monitor.sample()))
            except Exception:                              # noqa: BLE001 — a reading, not a crash
                self.events.put(('monitor-error', 'Some readings are unavailable for the moment.'))
            self.finished.wait(2 if self.controller.mode == 'power-saving' else 1)

    def power_loop(self):
        while not self.finished.is_set():
            self.events.put(('power', read_power()))
            self.finished.wait(15)

    def pump(self):
        """Empties the queue on Tk's own thread, the only one that may touch
        the window."""
        if self.finished.is_set():
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'sample':
                self.display(value)
                if not getattr(self, '_grown_for_readings', True):
                    self._grown_for_readings = True
                    self._grow_to_fit()
            elif kind == 'power':
                self.cards['power'][0].set(f'{value:.1f} W' if value is not None else 'Unavailable')
                self.cards['power'][1].set('Whole-device battery discharge, not Ninaivu alone' if value is not None
                                           else 'On AC power, or no discharge sensor; nothing is estimated')
            elif kind == 'appearance':
                self.system_dark = value
                if self.following_system:
                    self.apply_theme()
            elif kind in ('done', 'error'):
                self.operation_progress.stop()
                self.operation_progress.configure(style='Operation.Horizontal.TProgressbar')
                self.active_mode.set(self.mode_line())
                self.busy = False
                self.operation_label = ''
                running = bool(self.controller.record())
                self.set_controls(running)
                self.show_status(running)
                self.endpoints_info.set(self.format_endpoints())
                self.notice.set(value)
                if kind == 'error':
                    messagebox.showerror('Ninaivu Control Panel', value, parent=self.root)
            else:
                self.notice.set(value)
        self.root.after(150, self.pump)

    def _update_bar(self, canvas, value, max_val):
        p = self.palette
        canvas.reading = value
        try:
            canvas.delete('all')
            w = canvas.winfo_width()
            if w <= 1:
                w = 200
            h = 4
            fill_w = max(0, min(w, int(w * (value / max(1, max_val)))))
            color = p['danger'] if value > 85 else p['gold'] if value > 70 else p['accent']
            canvas.create_rectangle(0, 0, w, h, fill=p['sunken'], outline='')
            if fill_w > 0:
                canvas.create_rectangle(0, 0, fill_w, h, fill=color, outline='')
        except tk.TclError:
            pass

    def _draw_chart(self):
        p = self.palette
        try:
            self.chart.delete('all')
            w = max(100, self.chart.winfo_width())
            h = max(40, self.chart.winfo_height()) if self.chart.winfo_height() > 10 else 64
            pad_left = 34                                  # room for the percentages
            top, bottom = 6, h - 4
            chart_w = w - pad_left - 8
            if self.cpu_history:
                self.chart_title.set(f"Last 60 samples  ·  now {self.cpu_history[-1]:.0f}%  ·  "
                                     f"peak {max(self.cpu_history):.0f}%")

            def y_for(percent):
                return bottom - percent * (bottom - top) / 100

            for percent in (100, 50):
                y = y_for(percent)
                self.chart.create_text(pad_left - 8, y, text=f'{percent}%', anchor='e', font=self.fonts['axis'], fill=p['text3'])
                self.chart.create_line(pad_left, y, w, y, fill=p['border_soft'], dash=(2, 4))
            self.chart.create_text(pad_left - 8, bottom, text='0%', anchor='e', font=self.fonts['axis'], fill=p['text3'])
            self.chart.create_line(pad_left, bottom, w, bottom, fill=p['border'])
            points = [coordinate for i, v in enumerate(self.cpu_history)
                      for coordinate in (pad_left + i * chart_w / 59, y_for(v))]
            if len(points) >= 4:
                self.chart.create_polygon(pad_left, bottom, *points, points[-2], bottom, fill=p['chart_fill'], outline='')
                self.chart.create_line(*points, fill=p['accent'], width=2, smooth=True, capstyle='round', joinstyle='round')
                last_x, last_y = points[-2], points[-1]
                self.chart.create_oval(last_x - 4, last_y - 4, last_x + 4, last_y + 4,
                                       fill=p['accent'], outline=p['surface'], width=2)
        except tk.TclError:
            pass

    def display(self, s):
        is_running = s['running']
        if self.busy:
            self.status.set(self.operation_label + '…')
            self.show_status('busy')
        else:
            self.show_status(is_running)
            if is_running:
                # A server started elsewhere, or restarted from the console's
                # Server page into another mode: the radio follows it.
                try:
                    self.controller.capture_running_settings()
                except (KeyError, TypeError, OSError):
                    pass                                   # a run file half-read; next second's will do
                if self.mode.get() != self.controller.mode:
                    self.mode.set(self.controller.mode)
                    self._update_mode_selection()
            else:
                started = getattr(self.controller, 'started', None)
                if started is not None and started.poll() is not None:
                    self.controller.started = None
            self.active_mode.set(self.mode_line())

        self.endpoints_info.set(self.format_endpoints())
        self.set_controls(is_running)

        self.cards['cpu'][0].set(f"{s['cpu']:.0f}%")
        self.cards['cpu'][1].set('Across all applications and CPU cores')
        self._update_bar(self.card_bars['cpu'], s['cpu'], 100)

        self.cards['ram'][0].set(f"{s['ram_percent']:.0f}%")
        self.cards['ram'][1].set(f"{s['ram_used']/2**30:.1f} / {s['ram_total']/2**30:.1f} GB in use")
        self._update_bar(self.card_bars['ram'], s['ram_percent'], 100)

        self.cards['server'][0].set(f"{s['server_cpu']:.1f}% CPU" if is_running else 'Not running')
        self.cards['server'][1].set(f"{s['server_ram']/2**20:.0f} MB · {s['threads']} threads · {int(s['uptime']/60)} min up"
                                    if is_running else 'Start it here, from the tray, or with the launcher')
        self.cards['battery'][0].set('No battery' if s['battery'] is None else f"{s['battery']:.0f}%")
        self.cards['battery'][1].set('Battery data unavailable' if s['plugged'] is None
                                     else 'Connected to power' if s['plugged'] else 'Running on battery')
        self.cards['disk'][0].set(f"{s['disk_free']/2**30:.0f} GB")
        self.cards['disk'][1].set('On the drive Ninaivu is installed on')

        self.cpu_history = (self.cpu_history + [s['cpu']])[-60:]
        self._draw_chart()

    # -- the log pane, shown and hidden ---------------------------------------------

    def toggle_logs(self):
        if self.logs_visible:
            self.hide_logs()
        else:
            self.show_logs()

    def show_logs(self):
        if not self.logs_visible:
            root = self.root
            self._height_before_logs = root.winfo_height() if root.winfo_viewable() else None
            self._height_with_logs = None
            self.split.add(self.log_panel, minsize=180, height=260, stretch='never')
            self.logs_visible = True
            self._grow_to_fit(force=True)
            self.log_button_text.set('Hide logs')
        self.log_paused.set(False)
        self.log_follow.set(True)
        self._update_log_display()
        self.log_text.focus_set()
        self.log_text.see('end')

    def hide_logs(self):
        if self.logs_visible:
            # What the pane takes from the window, the divider included.
            self.root.update_idletasks()
            pane_height = max(0, self.split.winfo_height() - self.upper_pane.winfo_height())
            self.split.forget(self.log_panel)
            self.logs_visible = False
            self.log_button_text.set('View logs')
            self._shrink_after_logs(getattr(self, '_height_before_logs', None),
                                    getattr(self, '_height_with_logs', None), pane_height)

    def open_logs(self):
        self.show_logs()

    # -- sign-in and the certificate ------------------------------------------------

    def toggle_start_at_sign_in(self):
        wanted = self.start_at_sign_in.get()
        try:
            self.notice.set(autostart.enable(self.controller.root) if wanted else autostart.disable())
        except (OSError, RuntimeError) as exc:
            self.start_at_sign_in.set(autostart.enabled())
            messagebox.showerror('Start when I sign in', str(exc), parent=self.root)

    def open_certificate(self):
        """Trust Ninaivu's own certificate on this computer.

        The steps are the tray's (:meth:`Tray.trust_certificate`), run here on
        Tk's thread so its questions are this window's dialogs: a custom
        certificate is left alone, nothing is trusted without asking, and when
        trusting fails the certificate is shown with the step to do by hand.
        """
        from .tray import Tray
        helper = Tray(self.controller, run_async=False,
                      notify=lambda _title, message: self.notice.set(message),
                      ask=lambda title, question: messagebox.askyesno(title, question, parent=self.root),
                      open_url=webbrowser.open)
        helper.trust_certificate()

    def close(self):
        if self.busy:
            messagebox.showinfo('Ninaivu Control Panel', 'Wait for the current start or stop to finish.', parent=self.root)
            return
        self.finished.set()
        self.root.destroy()


def line_tag(line):
    """The colour a log line is shown in: errors red, warnings amber, the
    lines that say where Ninaivu answers blue."""
    lower = line.lower()
    if any(word in lower for word in ('error', 'traceback', 'exception', 'failed', 'crit')):
        return 'error'
    if 'warn' in lower:
        return 'warning'
    if any(word in lower for word in ('https://', 'http://', 'family', 'console', 'started', 'ready', 'listening')):
        return 'info'
    return ''


def missing_requirement():
    """What stops the panel from working here, in a sentence, or None."""
    if control.psutil is None:
        return ('The Control Panel needs psutil to see whether Ninaivu is running; '
                'without it, it would call a running server stopped.')
    return None


def main():
    if tk is None:
        # A Python without Tk cannot show the panel; the tray does the same
        # job from a menu, so the person is not left with nothing.
        print('The Control Panel needs Tk, which this Python does not have; opening the tray instead.', file=sys.stderr)
        from .tray import main as tray_main
        return tray_main()
    if os.name == 'nt':
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    # The web app's mark instead of Tk's feather, and on a Mac instead of
    # Python's rocket in the Dock, where the panel otherwise looks like any
    # other script.
    try:
        root.icon = tk.PhotoImage(file=str(ICON))
        root.iconphoto(True, root.icon)
    except tk.TclError:
        pass
    problem = missing_requirement()
    if problem:
        root.withdraw()
        messagebox.showerror('Ninaivu Control Panel', f'{problem}\n\n{NEEDS}', parent=root)
        root.destroy()
        return 2
    Dashboard(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
