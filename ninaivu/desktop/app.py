"""Ninaivu's Control Panel: a window of its own beside the tray.

    python -m ninaivu.desktop.app
    Start - Ninaivu Control Panel.vbs      (Windows, double-click)
    Ninaivu Control Panel.app              (macOS, built by Setup Ninaivu.command)

The tray (:mod:`ninaivu.desktop.tray`) is one small menu; this is the whole
picture on one screen: whether Ninaivu is running and where, Start, Stop and
Restart, the computer's CPU, memory, battery and disk with a minute of CPU
history, the resource mode, and the server's log as it is written. Most of
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
from . import control
from .control import Controller, Monitor, read_power
from .logs import LogTail
from ..utils.resources import budget

ICON = Path(__file__).resolve().parents[1] / 'static' / 'icons' / 'icon-192.png'

#: What to install when the panel cannot run, said the same way everywhere.
NEEDS = ('Install what the Control Panel needs into Ninaivu’s environment:\n\n'
         '    pip install -r requirements/requirements-desktop.txt\n\n'
         'or run the setup again (Setup Ninaivu.command on a Mac, start.cmd on Windows).')

BG = '#f1f5f9'
SURFACE = '#ffffff'
INK = '#0f172a'
MUTED = '#334155'
ACCENT = '#2563eb'
BORDER = '#e2e8f0'
SECTION_TITLE = '#1e293b'
SECTION_RULE = '#cbd5e1'

STATUS_RUNNING_FG = '#15803d'
STATUS_RUNNING_BG = '#f0fdf4'
STATUS_RUNNING_BORDER = '#86efac'
STATUS_STOPPED_FG = '#475569'
STATUS_STOPPED_BG = '#f8fafc'
STATUS_STOPPED_BORDER = '#cbd5e1'
STATUS_BUSY_FG = '#1d4ed8'
STATUS_BUSY_BG = '#eff6ff'
STATUS_BUSY_BORDER = '#93c5fd'

# The stripe down the left of each reading.
CARD_ACCENT = {
    'cpu':     '#3b82f6',   # blue
    'ram':     '#8b5cf6',   # purple
    'power':   '#f59e0b',   # amber
    'server':  '#10b981',   # emerald
    'battery': '#06b6d4',   # teal
    'disk':    '#64748b',   # slate
}

# The stripe along the top of each resource mode, and its tint when chosen.
MODE_ACCENT = {
    'standard':     '#3b82f6',  # blue
    'performance':  '#6366f1',  # indigo
    'power-saving': '#10b981',  # emerald
}
MODE_TINT = {
    'standard':     '#eff6ff',  # blue-50
    'performance':  '#eef2ff',  # indigo-50
    'power-saving': '#ecfdf5',  # emerald-50
}
MODE_ICON = {
    'standard':     '⚡',
    'performance':  '🚀',
    'power-saving': '🔋',
}

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

        root.configure(bg=BG)

        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TButton', font=('Segoe UI', 10, 'bold'), padding=(8, 4), background=SURFACE, foreground=INK)
        style.configure('Accent.TButton', background=ACCENT, foreground='white')
        style.map('Accent.TButton', background=[('active', '#1d4ed8'), ('disabled', '#93c5fd')])
        style.configure('Start.TButton', background='#16a34a', foreground='white', font=('Segoe UI', 10, 'bold'), padding=(10, 4))
        style.map('Start.TButton', background=[('active', '#15803d'), ('disabled', '#86efac')])
        style.configure('Stop.TButton', background='#dc2626', foreground='white', font=('Segoe UI', 10, 'bold'), padding=(10, 4))
        style.map('Stop.TButton', background=[('active', '#b91c1c'), ('disabled', '#fca5a5')])
        style.configure('TRadiobutton', background=SURFACE, foreground=INK, font=('Segoe UI', 10), padding=3)
        style.configure('TCombobox', font=('Segoe UI', 10))
        style.configure('TEntry', font=('Segoe UI', 10))
        style.configure('TSeparator', background=SECTION_RULE)

        command = tk.Frame(root, bg=BG, padx=16, pady=6)
        command.pack(fill='x')

        # The dashboard above, the log pane below it when shown; the divider
        # between them can be dragged.
        split = tk.PanedWindow(root, orient='vertical', bg=BG, sashwidth=6, borderwidth=0)
        split.pack(fill='both', expand=True)
        self.split = split

        upper = tk.Frame(split, bg=BG)
        split.add(upper, minsize=350, stretch='always')

        main = tk.Frame(upper, bg=BG, padx=16, pady=4)
        main.pack(fill='both', expand=True)

        # The first line: the name, whether it is running, and the mode it runs in.
        header = tk.Frame(command, bg=BG)
        header.pack(fill='x')

        brand = tk.Frame(header, bg=BG)
        brand.pack(side='left', anchor='w')
        tk.Label(brand, text='Ninaivu', font=('Segoe UI', 20, 'bold'), bg=BG, fg=INK).pack(side='left')
        badge_frame = tk.Frame(brand, bg='#fef3c7', highlightthickness=1, highlightbackground='#fcd34d', padx=6, pady=2)
        badge_frame.pack(side='left', padx=(8, 10))
        tk.Label(badge_frame, text='CONTROL PANEL', font=('Segoe UI', 8, 'bold'), bg='#fef3c7', fg='#92400e').pack()

        self.status = tk.StringVar(value='Checking server…')
        self.status_pill = tk.Frame(brand, bg=STATUS_STOPPED_BG, highlightthickness=1, highlightbackground=STATUS_STOPPED_BORDER, padx=10, pady=3)
        self.status_pill.pack(side='left')
        self.status_label = tk.Label(self.status_pill, textvariable=self.status, font=('Segoe UI', 11, 'bold'), bg=STATUS_STOPPED_BG, fg=STATUS_STOPPED_FG)
        self.status_label.pack()

        self.active_mode = tk.StringVar(value=self.mode_line())
        mode_pill = tk.Frame(header, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER, padx=10, pady=3)
        mode_pill.pack(side='right', anchor='e')
        tk.Label(mode_pill, textvariable=self.active_mode, font=('Segoe UI', 10, 'bold'), bg=SURFACE, fg=MUTED).pack()

        # The second: what can be done now, and the addresses it answers on.
        action_row = tk.Frame(command, bg=BG)
        action_row.pack(fill='x', pady=(4, 2))

        actions = tk.Frame(action_row, bg=BG)
        actions.pack(side='left', anchor='w')
        self.start_button = self._action_button(actions, '▶  Start', lambda: self.run(self.controller.start, 'Starting'), 'Start.TButton')
        self.stop_button = self._action_button(actions, '■  Stop', lambda: self.run(self.controller.stop, 'Stopping'), 'Stop.TButton')
        self.restart_button = self._action_button(actions, '↻  Restart', lambda: self.run(self.restart, 'Restarting'), 'Accent.TButton')
        tk.Frame(actions, bg=SECTION_RULE, width=1).pack(side='left', fill='y', padx=8, pady=2)
        self.button(actions, 'Open the family app', lambda: webbrowser.open(self.controller.server_url()))
        self.button(actions, 'Open the console', lambda: webbrowser.open(self.controller.server_url(True)))

        self.endpoints_info = tk.StringVar(value=self.format_endpoints())
        self.endpoints_label = tk.Label(action_row, textvariable=self.endpoints_info, font=('Segoe UI', 10), bg=BG, fg=MUTED, justify='right', anchor='e')
        self.endpoints_label.pack(side='right', fill='x', expand=True, anchor='e', padx=(8, 0))
        # On one line, the addresses of a running server are wider than the rest
        # of the panel together, and the first window was sized to them: nearly
        # the whole screen wide. They wrap to the room the buttons leave instead.
        self.endpoints_label.configure(wraplength=self._scaled(420))
        self.endpoints_label.bind('<Configure>', lambda e: self.endpoints_label.configure(
            wraplength=max(self._scaled(240), e.width)))

        self.operation_progress = ttk.Progressbar(command, mode='indeterminate')
        self.operation_progress.pack(fill='x', pady=(2, 0))

        # -- this computer ----------------------------------------------------
        section_header = tk.Frame(main, bg=BG)
        section_header.pack(fill='x', pady=(0, 4))
        tk.Label(section_header, text='📊  This computer', font=('Segoe UI', 11, 'bold'), bg=BG, fg=SECTION_TITLE).pack(side='left')

        grid = tk.Frame(main, bg=BG)
        grid.pack(fill='x')
        grid.columnconfigure((0, 1, 2), weight=1)

        self.card_bars = {}
        card_defs = [
            ('cpu', 'COMPUTER CPU'),
            ('ram', 'COMPUTER MEMORY'),
            ('power', 'BATTERY POWER DRAW'),
            ('server', 'NINAIVU PROCESS'),
            ('battery', 'BATTERY'),
            ('disk', 'FREE DISK SPACE'),
        ]
        for index, (key, title) in enumerate(card_defs):
            accent = CARD_ACCENT.get(key, ACCENT)
            outer = tk.Frame(grid, bg=accent, padx=0, pady=0)
            outer.grid(row=index // 3, column=index % 3, sticky='nsew', padx=(0, 8) if index % 3 != 2 else 0, pady=(0, 6))

            frame = tk.Frame(outer, bg=SURFACE, padx=10, pady=6, highlightthickness=1, highlightbackground=BORDER)
            frame.pack(side='right', fill='both', expand=True, padx=(3, 0))

            tk.Label(frame, text=title, font=('Segoe UI', 8, 'bold'), bg=SURFACE, fg=MUTED).pack(anchor='w')
            value = tk.StringVar(value='—')
            detail = tk.StringVar(value='Waiting for readings')
            tk.Label(frame, textvariable=value, font=('Segoe UI', 18, 'bold'), bg=SURFACE, fg=INK).pack(anchor='w', pady=(1, 1))

            if key in ('cpu', 'ram'):
                bar = tk.Canvas(frame, height=5, bg='#e2e8f0', highlightthickness=0)
                bar.pack(fill='x', pady=(1, 2))
                self.card_bars[key] = bar

            tk.Label(frame, textvariable=detail, font=('Segoe UI', 9), bg=SURFACE, fg=MUTED, wraplength=260, justify='left').pack(anchor='w')
            self.cards[key] = (value, detail)

        ttk.Separator(main, orient='horizontal').pack(fill='x', pady=(4, 4))

        # -- a minute of CPU ------------------------------------------------------
        chart_header = tk.Frame(main, bg=BG)
        chart_header.pack(fill='x', pady=(1, 3))
        self.chart_title = tk.StringVar(value='CPU activity · last 60 samples')
        tk.Label(chart_header, text='📈', font=('Segoe UI', 11), bg=BG).pack(side='left', padx=(0, 5))
        tk.Label(chart_header, textvariable=self.chart_title, font=('Segoe UI', 10, 'bold'), bg=BG, fg=MUTED).pack(side='left')

        chart_container = tk.Frame(main, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        chart_container.pack(fill='x')
        self.chart = tk.Canvas(chart_container, height=60, bg=SURFACE, highlightthickness=0)
        self.chart.pack(fill='x')

        ttk.Separator(main, orient='horizontal').pack(fill='x', pady=(4, 4))

        # -- the resource mode ------------------------------------------------
        mode_header = tk.Frame(main, bg=BG)
        mode_header.pack(fill='x', anchor='w', pady=(1, 3))
        tk.Label(mode_header, text='⚙️', font=('Segoe UI', 11), bg=BG).pack(side='left', padx=(0, 5))
        tk.Label(mode_header, text='Resource mode', font=('Segoe UI', 11, 'bold'), bg=BG, fg=SECTION_TITLE).pack(side='left')
        profiles = tk.Frame(main, bg=BG)
        profiles.pack(fill='x')
        profiles.columnconfigure((0, 1, 2), weight=1)
        self.radios = []
        self.mode_cards = {}
        for i, (mode, title, description) in enumerate(MODES):
            accent_color = MODE_ACCENT.get(mode, ACCENT)
            icon = MODE_ICON.get(mode, '')
            outer = tk.Frame(profiles, bg=accent_color, padx=0, pady=0)
            outer.grid(row=0, column=i, sticky='nsew', padx=(0, 8) if i < 2 else 0)

            card = tk.Frame(outer, bg=SURFACE, padx=10, pady=5, highlightthickness=1, highlightbackground=BORDER)
            card.pack(side='bottom', fill='both', expand=True, pady=(2, 0))

            radio = ttk.Radiobutton(card, text=f'{icon}  {title}', value=mode, variable=self.mode, command=self._update_mode_selection)
            radio.pack(anchor='w')
            self.radios.append(radio)
            tk.Label(card, text=description, font=('Segoe UI', 9), bg=SURFACE, fg=MUTED, wraplength=250, justify='left').pack(anchor='w', padx=4, pady=(1, 2))
            hint_badge = tk.Frame(card, bg='#f8fafc', highlightthickness=1, highlightbackground='#e2e8f0', padx=5, pady=1)
            hint_badge.pack(anchor='w', padx=4, pady=(1, 0))
            tk.Label(hint_badge, text=budget_hint(mode), font=('Segoe UI', 8, 'bold'), bg='#f8fafc', fg='#475569').pack()
            self.mode_cards[mode] = (outer, card)

        self.mode_description = tk.StringVar()
        self._update_mode_selection()
        tk.Label(main, textvariable=self.mode_description, font=('Segoe UI', 10), bg=BG, fg=MUTED, wraplength=950, justify='left').pack(anchor='w', pady=(3, 2))

        mode_actions = tk.Frame(main, bg=BG)
        mode_actions.pack(fill='x', pady=(2, 0))
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
        tk.Label(main, textvariable=self.notice, font=('Segoe UI', 9), bg=BG, fg=MUTED, wraplength=950, justify='left').pack(anchor='w', pady=(3, 0))

        self.build_logs(split)
        self._fit_window()
        root.protocol('WM_DELETE_WINDOW', self.close)
        threading.Thread(target=self.monitor_loop, daemon=True, name='ninaivu-panel-monitor').start()
        threading.Thread(target=self.power_loop, daemon=True, name='ninaivu-panel-power').start()
        root.after(100, self.pump)
        root.after(200, self.poll_logs)

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
        panel = tk.Frame(split, bg='#0d1522', padx=14, pady=8)
        self.log_panel = panel

        toolbar = tk.Frame(panel, bg='#0d1522')
        toolbar.pack(fill='x', pady=(0, 8))

        left = tk.Frame(toolbar, bg='#0d1522')
        left.pack(side='left')
        title_badge = tk.Frame(left, bg='#0f2d1c', highlightthickness=1, highlightbackground='#166534', padx=10, pady=4)
        title_badge.pack(side='left', padx=(0, 12))
        tk.Label(title_badge, text='●  LIVE LOGS', bg='#0f2d1c', fg='#4ade80', font=('Segoe UI', 10, 'bold')).pack()

        self.log_source = tk.StringVar(value='Server')
        source = ttk.Combobox(left, textvariable=self.log_source, values=['Server', 'Local AI'], state='readonly', width=10, font=('Segoe UI', 11))
        source.pack(side='left', padx=(0, 8))
        source.bind('<<ComboboxSelected>>', lambda _: self.switch_log())

        tk.Frame(left, bg='#334155', width=1).pack(side='left', fill='y', padx=8, pady=2)

        self.log_follow = tk.BooleanVar(value=True)
        tk.Checkbutton(left, text='Auto-scroll', variable=self.log_follow, bg='#0d1522', fg='#cbd5e1', selectcolor='#1e293b', activebackground='#0d1522', activeforeground='white', font=('Segoe UI', 11)).pack(side='left', padx=5)

        self.log_paused = tk.BooleanVar(value=False)
        tk.Checkbutton(left, text='Pause', variable=self.log_paused, bg='#0d1522', fg='#cbd5e1', selectcolor='#1e293b', activebackground='#0d1522', activeforeground='white', font=('Segoe UI', 11)).pack(side='left', padx=5)

        right = tk.Frame(toolbar, bg='#0d1522')
        right.pack(side='right')

        ttk.Button(right, text='✕ Hide', command=self.hide_logs).pack(side='right', padx=(6, 2))
        ttk.Button(right, text='Clear view', command=self.clear_log_view).pack(side='right', padx=2)
        ttk.Button(right, text='Copy view', command=self.copy_logs).pack(side='right', padx=2)
        tk.Frame(right, bg='#334155', width=1).pack(side='right', fill='y', padx=6, pady=2)
        ttk.Button(right, text='Find next', command=self.find_log).pack(side='right', padx=2)
        self.log_query = tk.StringVar()
        search = ttk.Entry(right, textvariable=self.log_query, font=('Segoe UI', 11), width=16)
        search.pack(side='right', padx=(4, 2))
        search.bind('<Return>', lambda _: self.find_log())
        tk.Label(right, text='Find:', bg='#0d1522', fg='#94a3b8', font=('Segoe UI', 11, 'bold')).pack(side='right', padx=(0, 4))

        holder = tk.Frame(panel, bg='#0d1522')
        holder.pack(fill='both', expand=True)

        self.log_text = tk.Text(holder, bg='#0b111c', fg='#cbd5e1', insertbackground='white', font=('Cascadia Mono', 11), wrap='word', height=9, borderwidth=0, state='disabled', padx=10, pady=10, spacing1=1, spacing3=1)
        scroll = ttk.Scrollbar(holder, command=self.log_text.yview)
        scroll.pack(side='right', fill='y')
        self.log_text.pack(side='left', fill='both', expand=True)
        self.log_text.configure(yscrollcommand=scroll.set)

        self.log_text.tag_configure('error', foreground='#f87171')
        self.log_text.tag_configure('warning', foreground='#fbbf24')
        self.log_text.tag_configure('info', foreground='#38bdf8')
        self.log_text.tag_configure('success', foreground='#4ade80')
        self.log_text.tag_configure('match', background='#fde047', foreground='#0f172a')

        self.log_info = tk.StringVar(value='Waiting for server output. Log files stay on this computer.')
        tk.Label(panel, textvariable=self.log_info, bg='#0d1522', fg='#94a3b8', font=('Segoe UI', 10)).pack(anchor='w', pady=(4, 0))
        self.log_tail = LogTail(self.log_path())

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
        """Tints the chosen mode's card and says what it gives."""
        selected = self.mode.get()
        for mode, (_outer, card) in self.mode_cards.items():
            chosen = mode == selected
            tint = MODE_TINT.get(mode, SURFACE) if chosen else SURFACE
            card.configure(bg=tint, highlightbackground=MODE_ACCENT.get(mode, ACCENT) if chosen else BORDER,
                           highlightthickness=2 if chosen else 1)
            for child in card.winfo_children():
                try:
                    child.configure(bg=tint)
                except tk.TclError:
                    pass
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
        self.status_pill.configure(bg=STATUS_BUSY_BG, highlightbackground=STATUS_BUSY_BORDER)
        self.status_label.configure(bg=STATUS_BUSY_BG, fg=STATUS_BUSY_FG)
        self.set_controls()
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
        if running:
            self.status_pill.configure(bg=STATUS_RUNNING_BG, highlightbackground=STATUS_RUNNING_BORDER)
            self.status_label.configure(bg=STATUS_RUNNING_BG, fg=STATUS_RUNNING_FG)
            self.status.set('● Running')
        else:
            self.status_pill.configure(bg=STATUS_STOPPED_BG, highlightbackground=STATUS_STOPPED_BORDER)
            self.status_label.configure(bg=STATUS_STOPPED_BG, fg=STATUS_STOPPED_FG)
            self.status.set('○ Stopped')

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
            elif kind in ('done', 'error'):
                self.operation_progress.stop()
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
        try:
            canvas.delete('all')
            w = canvas.winfo_width()
            if w <= 1:
                w = 200
            h = 5
            fill_w = max(0, min(w, int(w * (value / max(1, max_val)))))
            color = '#ef4444' if value > 85 else '#f59e0b' if value > 70 else ACCENT
            canvas.create_rectangle(0, 0, w, h, fill='#e2e8f0', outline='')
            if fill_w > 0:
                canvas.create_rectangle(0, 0, fill_w, h, fill=color, outline='')
        except tk.TclError:
            pass

    def _draw_chart(self):
        try:
            self.chart.delete('all')
            w = max(100, self.chart.winfo_width())
            h = max(40, self.chart.winfo_height()) if self.chart.winfo_height() > 10 else 60
            pad_left = 32                                  # room for the percentages
            chart_w = w - pad_left
            if self.cpu_history:
                self.chart_title.set(f"CPU activity · last 60 samples (now {self.cpu_history[-1]:.0f}% · "
                                     f"peak {max(self.cpu_history):.0f}%)")
            for y_ratio, label in ((0.25, '75%'), (0.5, '50%'), (0.75, '25%')):
                y = int(h * y_ratio)
                self.chart.create_text(pad_left - 4, y, text=label, anchor='e', font=('Segoe UI', 7), fill='#94a3b8')
                self.chart.create_line(pad_left, y, w, y, fill='#e2e8f0', dash=(2, 4))
            self.chart.create_text(pad_left - 4, h - 3, text='0%', anchor='e', font=('Segoe UI', 7), fill='#94a3b8')
            points = [coordinate for i, v in enumerate(self.cpu_history)
                      for coordinate in (pad_left + i * chart_w / 59, h - 3 - v * (h - 6) / 100)]
            if len(points) >= 4:
                self.chart.create_polygon(pad_left, h, *points, points[-2], h, fill='#eff6ff', outline='')
                self.chart.create_line(*points, fill=ACCENT, width=2, smooth=True)
                last_x, last_y = points[-2], points[-1]
                self.chart.create_oval(last_x - 3, last_y - 3, last_x + 3, last_y + 3, fill='#1d4ed8', outline='white', width=1)
        except tk.TclError:
            pass

    def display(self, s):
        is_running = s['running']
        if self.busy:
            self.status.set(self.operation_label + '…')
            self.status_pill.configure(bg=STATUS_BUSY_BG, highlightbackground=STATUS_BUSY_BORDER)
            self.status_label.configure(bg=STATUS_BUSY_BG, fg=STATUS_BUSY_FG)
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
            self.split.forget(self.log_panel)
            self.logs_visible = False
            self.log_button_text.set('View logs')

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
