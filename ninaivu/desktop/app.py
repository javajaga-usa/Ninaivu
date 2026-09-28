"""Ninaivu's native desktop dashboard. Background operations never block Tk."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox
import webbrowser
from pathlib import Path

from . import autostart
from .control import Controller, Monitor, read_power
from .logs import LogTail
from ..utils.resources import budget

ICON = Path(__file__).resolve().parents[1] / 'static' / 'icons' / 'icon-192.png'

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

# Per-card accent stripe colors (left border)
CARD_ACCENT = {
    'cpu':     '#3b82f6',   # blue
    'ram':     '#8b5cf6',   # purple
    'power':   '#f59e0b',   # amber
    'server':  '#10b981',   # emerald
    'battery': '#06b6d4',   # teal
    'disk':    '#64748b',   # slate
}

# Resource-mode accent colors (top border)
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

        root.title('Ninaivu Control Center')

        # Configure system named fonts so all native widgets inherit legible sizing
        for font_name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
            try:
                f = tkfont.nametofont(font_name)
                if f.cget('size') < 11:
                    f.configure(size=11)
            except Exception:
                pass

        # Protect against scaling artificially squashed below true display density
        try:
            current_scale = float(root.tk.call('tk', 'scaling'))
            if current_scale < 1.3333:
                root.tk.call('tk', 'scaling', 1.3333)
        except Exception:
            pass

        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        init_w = max(880, min(1300, int(screen_w * 0.58)))
        init_h = max(660, min(900, int(screen_h * 0.70)))
        root.geometry(f"{init_w}x{init_h}")
        root.minsize(820, 620)
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

        split = tk.PanedWindow(root, orient='vertical', bg=BG, sashwidth=6, borderwidth=0)
        split.pack(fill='both', expand=True)
        self.split = split

        upper = tk.Frame(split, bg=BG)
        split.add(upper, minsize=350, stretch='always')

        # Main content directly inside upper — all fitted in one screen without main scrollbar
        main = tk.Frame(upper, bg=BG, padx=16, pady=4)
        main.pack(fill='both', expand=True)

        class _ViewportShim:
            def __init__(self, target):
                self._target = target
            def yview_moveto(self, *args):
                pass
            def yview_scroll(self, *args):
                pass
            def __getattr__(self, name):
                return getattr(self._target, name)

        self.viewport = _ViewportShim(main)

        # Header Row: Brand, badge, status pill on left; active mode on right
        header = tk.Frame(command, bg=BG)
        header.pack(fill='x')

        brand = tk.Frame(header, bg=BG)
        brand.pack(side='left', anchor='w')
        tk.Label(brand, text='Ninaivu', font=('Segoe UI', 20, 'bold'), bg=BG, fg=INK).pack(side='left')
        badge_frame = tk.Frame(brand, bg='#fef3c7', highlightthickness=1, highlightbackground='#fcd34d', padx=6, pady=2)
        badge_frame.pack(side='left', padx=(8, 10))
        tk.Label(badge_frame, text='CONTROL CENTER', font=('Segoe UI', 8, 'bold'), bg='#fef3c7', fg='#92400e').pack()

        self.status = tk.StringVar(value='Checking server…')
        self.status_pill = tk.Frame(brand, bg=STATUS_STOPPED_BG, highlightthickness=1, highlightbackground=STATUS_STOPPED_BORDER, padx=10, pady=3)
        self.status_pill.pack(side='left')
        self.status_label = tk.Label(self.status_pill, textvariable=self.status, font=('Segoe UI', 11, 'bold'), bg=STATUS_STOPPED_BG, fg=STATUS_STOPPED_FG)
        self.status_label.pack()

        self.active_mode = tk.StringVar(value='Active mode: ' + self.controller.mode.replace('-', ' ').title())
        mode_pill = tk.Frame(header, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER, padx=10, pady=3)
        mode_pill.pack(side='right', anchor='e')
        tk.Label(mode_pill, textvariable=self.active_mode, font=('Segoe UI', 10, 'bold'), bg=SURFACE, fg=MUTED).pack()

        # Row 2: Action buttons on left, endpoints indicator on right
        action_row = tk.Frame(command, bg=BG)
        action_row.pack(fill='x', pady=(4, 2))

        actions = tk.Frame(action_row, bg=BG)
        actions.pack(side='left', anchor='w')
        self.start_button = self._action_button(actions, '▶  Start server', lambda: self.run(self.controller.start, 'Starting'), 'Start.TButton')
        self.stop_button = self._action_button(actions, '■  Stop server', lambda: self.run(self.controller.stop, 'Stopping'), 'Stop.TButton')
        self.restart_button = self._action_button(actions, '↻  Restart', lambda: self.run(self.restart, 'Restarting'), 'Accent.TButton')
        tk.Frame(actions, bg=SECTION_RULE, width=1).pack(side='left', fill='y', padx=8, pady=2)
        self.button(actions, 'Open Ninaivu', lambda: webbrowser.open(self.controller.server_url()))
        self.button(actions, 'Admin console', lambda: webbrowser.open(self.controller.server_url(True)))

        self.endpoints_info = tk.StringVar(value=self.format_endpoints())
        self.endpoints_label = tk.Label(action_row, textvariable=self.endpoints_info, font=('Segoe UI', 10), bg=BG, fg=MUTED, justify='right', anchor='e')
        self.endpoints_label.pack(side='right', fill='x', expand=True, anchor='e', padx=(8, 0))

        self.operation_progress = ttk.Progressbar(command, mode='indeterminate')
        self.operation_progress.pack(fill='x', pady=(2, 0))

        # ── System Metrics ─────────────────────────────────────────────
        section_header = tk.Frame(main, bg=BG)
        section_header.pack(fill='x', pady=(0, 4))
        tk.Label(section_header, text='📊  System Metrics', font=('Segoe UI', 11, 'bold'), bg=BG, fg=SECTION_TITLE).pack(side='left')

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
            ('disk', 'FREE DISK SPACE')
        ]
        for index, (key, title) in enumerate(card_defs):
            accent = CARD_ACCENT.get(key, ACCENT)
            outer = tk.Frame(grid, bg=accent, padx=0, pady=0)
            outer.grid(row=index // 3, column=index % 3, sticky='nsew', padx=(0, 8) if index % 3 != 2 else 0, pady=(0, 6))

            frame = tk.Frame(outer, bg=SURFACE, padx=10, pady=6, highlightthickness=1, highlightbackground=BORDER)
            frame.pack(side='right', fill='both', expand=True, padx=(3, 0))

            title_label = tk.Label(frame, text=title, font=('Segoe UI', 8, 'bold'), bg=SURFACE, fg=MUTED)
            title_label.pack(anchor='w')
            value = tk.StringVar(value='—')
            detail = tk.StringVar(value='Waiting for readings')
            value_label = tk.Label(frame, textvariable=value, font=('Segoe UI', 18, 'bold'), bg=SURFACE, fg=INK)
            value_label.pack(anchor='w', pady=(1, 1))

            if key in ('cpu', 'ram'):
                bar = tk.Canvas(frame, height=5, bg='#e2e8f0', highlightthickness=0)
                bar.pack(fill='x', pady=(1, 2))
                self.card_bars[key] = bar

            tk.Label(frame, textvariable=detail, font=('Segoe UI', 9), bg=SURFACE, fg=MUTED, wraplength=260, justify='left').pack(anchor='w')
            self.cards[key] = (value, detail)

        # ── Section divider ────────────────────────────────────────────
        ttk.Separator(main, orient='horizontal').pack(fill='x', pady=(4, 4))

        # CPU Activity Chart section
        chart_header = tk.Frame(main, bg=BG)
        chart_header.pack(fill='x', pady=(1, 3))
        self.chart_title = tk.StringVar(value='CPU activity · last 60 samples')
        tk.Label(chart_header, text='📈', font=('Segoe UI', 11), bg=BG).pack(side='left', padx=(0, 5))
        tk.Label(chart_header, textvariable=self.chart_title, font=('Segoe UI', 10, 'bold'), bg=BG, fg=MUTED).pack(side='left')

        chart_container = tk.Frame(main, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        chart_container.pack(fill='x')
        self.chart = tk.Canvas(chart_container, height=60, bg=SURFACE, highlightthickness=0)
        self.chart.pack(fill='x')

        # ── Section divider ────────────────────────────────────────────
        ttk.Separator(main, orient='horizontal').pack(fill='x', pady=(4, 4))

        # Resource Modes section
        mode_header = tk.Frame(main, bg=BG)
        mode_header.pack(fill='x', anchor='w', pady=(1, 3))
        tk.Label(mode_header, text='⚙️', font=('Segoe UI', 11), bg=BG).pack(side='left', padx=(0, 5))
        tk.Label(mode_header, text='Resource Mode', font=('Segoe UI', 11, 'bold'), bg=BG, fg=SECTION_TITLE).pack(side='left')
        profiles = tk.Frame(main, bg=BG)
        profiles.pack(fill='x')
        profiles.columnconfigure((0, 1, 2), weight=1)
        self.radios = []
        self.mode_cards = {}
        for i, (mode, title, description, budget_hint) in enumerate([
            ('standard', 'Standard', 'Balanced resources for daily use.', '4 workers · 4 AI threads'),
            ('performance', 'Performance', 'More threads for intensive work.', '8 workers · 8 AI threads'),
            ('power-saving', 'Power-saving', 'Fewer threads and efficient scheduling.', '1 worker · 2 AI threads · Throttled')
        ]):
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
            tk.Label(hint_badge, text=budget_hint, font=('Segoe UI', 8, 'bold'), bg='#f8fafc', fg='#475569').pack()
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
        self.button(mode_actions, 'Install HTTPS certificate', self.open_certificate)
        if autostart.supported():
            # So a restart for an update, or after a power cut, brings the
            # family app back without anybody opening Ninaivu.
            self.start_at_sign_in = tk.BooleanVar(value=autostart.enabled())
            ttk.Checkbutton(mode_actions, text='Start Ninaivu when I sign in',
                            variable=self.start_at_sign_in,
                            command=self.toggle_start_at_sign_in).pack(side='left', padx=(8, 0))

        self.notice = tk.StringVar(value='Applying a mode gracefully restarts a running server. Closing this panel leaves Ninaivu running.')
        tk.Label(main, textvariable=self.notice, font=('Segoe UI', 9), bg=BG, fg=MUTED, wraplength=950, justify='left').pack(anchor='w', pady=(3, 0))

        self.build_logs(split)
        root.protocol('WM_DELETE_WINDOW', self.close)
        threading.Thread(target=self.monitor_loop, daemon=True, name='ninaivu-dashboard-monitor').start()
        threading.Thread(target=self.power_loop, daemon=True, name='ninaivu-dashboard-power').start()
        root.after(100, self.pump)
        root.after(200, self.poll_logs)

    def format_endpoints(self):
        record = self.controller.record()
        if record:
            scheme = (record.get('scheme') or 'http').upper()
            port = record.get('port', 443 if scheme == 'HTTPS' else 80)
            pid = record.get('pid', '—')
            return f"Family: {self.controller.server_url()}  ·  Console: {self.controller.server_url(True)}  ·  {scheme} :{port} (PID {pid})"
        target = self.controller.server_url()
        cfg = getattr(self.controller, 'cfg', None)
        state_dir = getattr(cfg, 'state_dir', None)
        state_str = f"  ·  State: {state_dir.name}" if state_dir else ""
        return f"Offline  ·  Target: {target}{state_str}"

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

        # Separator between source and toggles
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
        # Separator between actions and search
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
        self.log_tail = LogTail(self.controller.runtime / 'server.log')

    def clear_log_view(self):
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
            self.log_info.set('Match found · Auto-scroll paused so you can read. Enable Auto-scroll to follow live output.')
        else:
            self.log_info.set('No match in the currently displayed logs.')

    def copy_logs(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get('1.0', 'end-1c'))
        self.log_info.set('Displayed logs copied to clipboard. Review private paths and details before sharing.')

    def switch_log(self):
        if self.log_source.get() == 'Server':
            log_path = self.controller.runtime / 'server.log'
        else:
            primary = self.controller.runtime / 'ollama.log'
            fallback = self.controller.root / '.ai-models' / 'ollama-stdout.log'
            log_path = primary if primary.exists() or not fallback.exists() else fallback
        self.log_tail = LogTail(log_path)
        self.clear_log_view()
        self.poll_logs()

    def _update_log_display(self):
        try:
            if self.log_source.get() == 'Local AI' and not self.log_tail.path.exists():
                fallback = self.controller.root / '.ai-models' / 'ollama-stdout.log'
                primary = self.controller.runtime / 'ollama.log'
                if fallback.exists() and not primary.exists():
                    self.log_tail = LogTail(fallback)
            text = self.log_tail.read()
            if text:
                self.log_text.configure(state='normal')
                for line in text.splitlines(keepends=True):
                    lower = line.lower()
                    if any(word in lower for word in ('error', 'traceback', 'exception', 'failed', 'crit')):
                        tag = 'error'
                    elif any(word in lower for word in ('warn', 'warning')):
                        tag = 'warning'
                    elif any(word in lower for word in ('https://', 'http://', 'family', 'admin', 'started', 'ready', 'listening')):
                        tag = 'info'
                    else:
                        tag = ''
                    self.log_text.insert('end', line, tag)
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
                self.log_info.set(f"{self.log_source.get()} · Live output · Display limited to 2,000 lines / 200 KB")
            elif not self.log_tail.path.exists():
                self.log_info.set('No log file yet. Start the service to see output here.')
        except OSError as error:
            self.log_info.set(f'Log temporarily unavailable: {error.strerror or "read error"}')

    def poll_logs(self):
        if self.finished.is_set():
            return
        if not self.log_paused.get():
            self._update_log_display()
        self.root.after(750, self.poll_logs)

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
        """Create a colored action button with a specific ttk style."""
        button = ttk.Button(parent, text=text, command=command, style=style)
        button.pack(side='left', padx=(0, 8))
        self.buttons.append(button)
        return button

    def _update_mode_selection(self):
        """Highlight the selected resource mode card and update the description."""
        selected = self.mode.get()
        for mode, (_outer, card) in self.mode_cards.items():
            if mode == selected:
                tint = MODE_TINT.get(mode, SURFACE)
                accent = MODE_ACCENT.get(mode, ACCENT)
                card.configure(bg=tint, highlightbackground=accent, highlightthickness=2)
                for child in card.winfo_children():
                    try:
                        child.configure(bg=tint)
                    except tk.TclError:
                        pass
            else:
                card.configure(bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
                for child in card.winfo_children():
                    try:
                        child.configure(bg=SURFACE)
                    except tk.TclError:
                        pass
        self.describe_mode()

    def describe_mode(self):
        b = budget(self.mode.get())
        self.mode_description.set(f"Selected: {b['workers']} scan workers · {b['compute_threads']} AI/video threads · {b['server_threads']} request threads. Image resolution and quality stay unchanged.")

    def restart(self):
        self.controller.stop()
        return self.controller.start()

    def apply_selected_mode(self):
        selected = self.mode.get()
        self.run(lambda: self.controller.apply_mode(selected))

    def run(self, operation, label='Applying settings'):
        if self.busy:
            return
        self.busy = True
        self.operation_label = label
        self.status.set(label + '…')
        self.notice.set(label + '… Ninaivu will finish active files before stopping.')
        self.status_pill.configure(bg=STATUS_BUSY_BG, highlightbackground=STATUS_BUSY_BORDER)
        self.status_label.configure(bg=STATUS_BUSY_BG, fg=STATUS_BUSY_FG)
        self.set_controls()
        self.operation_progress.start(12)

        def worker():
            try:
                self.events.put(('done', operation()))
            except Exception as error:
                self.events.put(('error', str(error)))

        threading.Thread(target=worker, daemon=True, name='ninaivu-control-action').start()

    def set_controls(self, running=None):
        for widget in [*self.buttons, *self.radios]:
            widget.configure(state='disabled' if self.busy else 'normal')
        if not self.busy and running is not None:
            self.start_button.configure(state='disabled' if running else 'normal')
            self.stop_button.configure(state='normal' if running else 'disabled')
            self.restart_button.configure(state='normal' if running else 'disabled')

    def monitor_loop(self):
        while not self.finished.is_set():
            try:
                self.events.put(('sample', self.monitor.sample()))
            except Exception:
                self.events.put(('monitor-error', 'Some system readings are temporarily unavailable.'))
            self.finished.wait(2 if self.mode_snapshot() == 'power-saving' else 1)

    def mode_snapshot(self):
        return self.controller.mode

    def power_loop(self):
        while not self.finished.is_set():
            self.events.put(('power', read_power()))
            self.finished.wait(15)

    def pump(self):
        if self.finished.is_set():
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'sample':
                self.display(value)
            elif kind == 'power':
                self.cards['power'][0].set(f'{value:.1f} W' if value is not None else 'Unavailable')
                self.cards['power'][1].set('Whole-device battery discharge, not Ninaivu alone' if value is not None else 'AC power or no supported discharge sensor; no estimate shown')
            elif kind in ('done', 'error'):
                self.operation_progress.stop()
                self.active_mode.set('Active mode: ' + self.controller.mode.replace('-', ' ').title())
                self.busy = False
                self.operation_label = ''
                running = bool(self.controller.record())
                self.set_controls(running)
                if running:
                    self.status_pill.configure(bg=STATUS_RUNNING_BG, highlightbackground=STATUS_RUNNING_BORDER)
                    self.status_label.configure(bg=STATUS_RUNNING_BG, fg=STATUS_RUNNING_FG)
                    self.status.set('● Running')
                else:
                    self.status_pill.configure(bg=STATUS_STOPPED_BG, highlightbackground=STATUS_STOPPED_BORDER)
                    self.status_label.configure(bg=STATUS_STOPPED_BG, fg=STATUS_STOPPED_FG)
                    self.status.set('○ Stopped')
                self.endpoints_info.set(self.format_endpoints())
                self.notice.set(value)
                if kind == 'error':
                    messagebox.showerror('Ninaivu control', value, parent=self.root)
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
            # Background track
            canvas.create_rectangle(0, 0, w, h, fill='#e2e8f0', outline='')
            # Filled portion
            if fill_w > 0:
                canvas.create_rectangle(0, 0, fill_w, h, fill=color, outline='')
        except Exception:
            pass

    def _draw_chart(self):
        try:
            self.chart.delete('all')
            w = max(100, self.chart.winfo_width())
            h = max(40, self.chart.winfo_height()) if self.chart.winfo_height() > 10 else 60
            pad_left = 32  # space for percentage labels
            chart_w = w - pad_left
            if self.cpu_history:
                curr = self.cpu_history[-1]
                peak = max(self.cpu_history)
                self.chart_title.set(f"CPU activity · last 60 samples (current: {curr:.0f}% · peak: {peak:.0f}%)")
            # Horizontal gridlines with percentage labels
            for y_ratio, label in ((0.25, '75%'), (0.5, '50%'), (0.75, '25%')):
                y = int(h * y_ratio)
                self.chart.create_text(pad_left - 4, y, text=label, anchor='e', font=('Segoe UI', 7), fill='#94a3b8')
                self.chart.create_line(pad_left, y, w, y, fill='#e2e8f0', dash=(2, 4))
            # Bottom baseline label
            self.chart.create_text(pad_left - 4, h - 3, text='0%', anchor='e', font=('Segoe UI', 7), fill='#94a3b8')
            # Data points
            points = [coordinate for i, v in enumerate(self.cpu_history) for coordinate in (pad_left + i * chart_w / 59, h - 3 - v * (h - 6) / 100)]
            if len(points) >= 4:
                area_poly = [pad_left, h, *points, points[-2], h]
                self.chart.create_polygon(*area_poly, fill='#eff6ff', outline='')
                self.chart.create_line(*points, fill=ACCENT, width=2, smooth=True)
                last_x, last_y = points[-2], points[-1]
                self.chart.create_oval(last_x - 3, last_y - 3, last_x + 3, last_y + 3, fill='#1d4ed8', outline='white', width=1)
        except Exception:
            pass

    def display(self, s):
        is_running = s['running']
        if self.busy:
            status_text = self.operation_label + '…'
            self.status_pill.configure(bg=STATUS_BUSY_BG, highlightbackground=STATUS_BUSY_BORDER)
            self.status_label.configure(bg=STATUS_BUSY_BG, fg=STATUS_BUSY_FG)
        elif is_running:
            status_text = '● Running'
            self.status_pill.configure(bg=STATUS_RUNNING_BG, highlightbackground=STATUS_RUNNING_BORDER)
            self.status_label.configure(bg=STATUS_RUNNING_BG, fg=STATUS_RUNNING_FG)
            try:
                if hasattr(self.controller, 'capture_running_settings'):
                    self.controller.capture_running_settings()
            except Exception:
                pass
            current_mode = getattr(self.controller, 'mode', 'standard')
            if not self.busy and self.mode.get() != current_mode:
                self.mode.set(current_mode)
                self._update_mode_selection()
            self.active_mode.set('Active mode: ' + current_mode.replace('-', ' ').title())
        else:
            status_text = '○ Stopped'
            self.status_pill.configure(bg=STATUS_STOPPED_BG, highlightbackground=STATUS_STOPPED_BORDER)
            self.status_label.configure(bg=STATUS_STOPPED_BG, fg=STATUS_STOPPED_FG)
            started = getattr(self.controller, 'started', None)
            if started and hasattr(started, 'poll') and started.poll() is not None:
                self.controller.started = None
            current_mode = getattr(self.controller, 'mode', 'standard')
            self.active_mode.set('Active mode: ' + current_mode.replace('-', ' ').title())

        self.status.set(status_text)
        self.endpoints_info.set(self.format_endpoints())
        self.set_controls(is_running)

        self.cards['cpu'][0].set(f"{s['cpu']:.0f}%")
        self.cards['cpu'][1].set('Across all applications and CPU cores')
        if 'cpu' in self.card_bars:
            self._update_bar(self.card_bars['cpu'], s['cpu'], 100)

        self.cards['ram'][0].set(f"{s['ram_percent']:.0f}%")
        self.cards['ram'][1].set(f"{s['ram_used']/2**30:.1f} / {s['ram_total']/2**30:.1f} GB in use")
        if 'ram' in self.card_bars:
            self._update_bar(self.card_bars['ram'], s['ram_percent'], 100)

        self.cards['server'][0].set(f"{s['server_cpu']:.1f}% CPU")
        self.cards['server'][1].set(f"{s['server_ram']/2**20:.0f} MB · {s['threads']} threads · {int(s['uptime']/60)} min uptime")
        self.cards['battery'][0].set('No battery' if s['battery'] is None else f"{s['battery']:.0f}%")
        self.cards['battery'][1].set('Battery data unavailable' if s['plugged'] is None else 'Connected to power' if s['plugged'] else 'Running on battery')
        self.cards['disk'][0].set(f"{s['disk_free']/2**30:.0f} GB")
        self.cards['disk'][1].set('On the Ninaivu installation drive')

        self.cpu_history = (self.cpu_history + [s['cpu']])[-60:]
        self._draw_chart()

    def toggle_logs(self):
        if self.logs_visible:
            self.hide_logs()
        else:
            self.show_logs()

    def show_logs(self):
        if not self.logs_visible:
            self.split.add(self.log_panel, minsize=180, height=260, stretch='never')
            self.logs_visible = True
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

    def toggle_start_at_sign_in(self):
        wanted = self.start_at_sign_in.get()
        try:
            self.notice.set(autostart.enable() if wanted else autostart.disable())
        except (OSError, RuntimeError) as exc:
            self.start_at_sign_in.set(autostart.enabled())
            messagebox.showerror('Start when I sign in', str(exc), parent=self.root)

    def open_certificate(self):
        from ..utils.tls import ca_certificate_path
        args = self.controller.settings.get('arguments', [])
        if any(arg == '--cert' or str(arg).startswith('--cert=') for arg in args):
            messagebox.showinfo('HTTPS certificate', 'This server uses a custom certificate. Use the trust instructions from its issuer; an older Ninaivu certificate will not establish trust in it.', parent=self.root)
            return
        path = ca_certificate_path(self.controller.cfg.state_dir)
        if not path.is_file():
            messagebox.showinfo('HTTPS certificate', 'Start Ninaivu with HTTPS first to create its certificate.', parent=self.root)
            return
        if sys.platform == 'win32':
            from ..utils.tls import trust_ca_on_windows
            if not messagebox.askyesno(
                    'HTTPS certificate',
                    "Add Ninaivu's certificate to Trusted Root Certification Authorities for this "
                    'Windows account, so browsers on this computer open Ninaivu without a warning?\n\n'
                    'Windows will ask you to confirm. Only do this for your own Ninaivu.',
                    parent=self.root):
                return
            trusted, message = trust_ca_on_windows(path)
            if trusted:
                self.notice.set(message + ' Other devices need the certificate installed separately.')
                return
            # Fall back to the certificate window, saying plainly where it must go:
            # its default choice files it where browsers never trust it.
            os.startfile(path)
            self.notice.set(message + ' In the certificate window choose Install Certificate → Current User → '
                            '"Place all certificates in the following store" → Trusted Root Certification '
                            'Authorities. Leaving it on "Automatically select" puts it under Intermediate, '
                            'where it is not trusted.')
            return
        if sys.platform == 'darwin':
            # Importing through Keychain Access is where it went wrong: it offers
            # keychains that take no certificates, refuses one already there,
            # and leaves it untrusted either way. So it is trusted directly, as
            # Windows adds it to Root directly, and macOS asks for the password.
            from ..utils.tls import trust_ca_on_mac
            if not messagebox.askyesno(
                    'HTTPS certificate',
                    "Trust Ninaivu's certificate for websites on this Mac, so browsers open "
                    'Ninaivu without a warning?\n\nmacOS will ask for your password. Only do '
                    'this for your own Ninaivu.',
                    parent=self.root):
                return
            trusted, message = trust_ca_on_mac(path)
            if trusted:
                self.notice.set(message + ' Other devices need the certificate installed separately.')
                return
            subprocess.run(['open', '-R', str(path)], check=False)
            self.notice.set(message + ' To do it by hand: in Keychain Access choose the login '
                            'keychain, find "Ninaivu local CA" (or drag in the file just shown in '
                            'Finder), double-click it, open Trust and set "When using this '
                            'certificate" to Always Trust. If it says the certificate is already '
                            'there, it only needs that last step.')
            return
        webbrowser.open(path.as_uri())
        self.notice.set('Install the certificate as a trusted root certificate authority. Other devices need it installed separately.')

    def close(self):
        if self.busy:
            messagebox.showinfo('Ninaivu control', 'Wait for the current start or stop action to finish.', parent=self.root)
            return
        self.finished.set()
        self.root.destroy()


def main():
    if os.name == 'nt':
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    # The web app's mark instead of Tk's feather — and on a Mac instead of
    # Python's rocket in the Dock, where the panel otherwise looks like any
    # other script.
    try:
        root.icon = tk.PhotoImage(file=str(ICON))
        root.iconphoto(True, root.icon)
    except tk.TclError:
        pass
    Dashboard(root)
    root.mainloop()


if __name__ == '__main__':
    main()
