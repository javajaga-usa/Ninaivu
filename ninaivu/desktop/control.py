"""Native control panel backend, independent from the running HTTP server."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import psutil
    _PsutilError: type[Exception] = psutil.Error
except ImportError:
    psutil = None
    class _PsutilError(Exception):
        pass
from ..server.config import Config
from ..server import runfile
from ..utils.resources import budget, environment

ROOT = Path(__file__).resolve().parents[2]
HIDDEN = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


def discharge_watts(rows):
    """Only absolute mW sensors while discharging; no guessed CPU wattage."""
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        return None
    total = 0
    for row in rows:
        value = row.get('DischargeRate')
        if (row.get('PowerOnline') or not row.get('Discharging') or row.get('Relative')
                or not isinstance(value, (float, int)) or not 0 < value < 2_147_483_647):
            return None
        total += value
    return total / 1000


def read_power():
    if sys.platform != 'win32':
        return None
    # Query both status and units: some batteries report relative units, not mW.
    command = "$s=Get-CimInstance -Namespace root/wmi -Class BatteryStatus -ErrorAction Stop; $i=Get-CimInstance -Namespace root/wmi -Class BatteryStaticData -ErrorAction Stop; @($s | ForEach-Object { $b=$_; $u=$i | Where-Object InstanceName -EQ $b.InstanceName; [pscustomobject]@{ DischargeRate=$b.DischargeRate; PowerOnline=$b.PowerOnline; Discharging=$b.Discharging; Relative=(!$u -or (($u.Capabilities -band 1073741824) -ne 0)) } }) | ConvertTo-Json -Compress"
    try:
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                                capture_output=True,text=True,timeout=8,creationflags=HIDDEN)
        return discharge_watts(json.loads(result.stdout)) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


class Controller:
    def __init__(self, root=ROOT, cfg=None):
        self.root = Path(root)
        self.cfg = cfg or Config.load()
        self.runtime = self.root / '.ninaivu-control'
        self.settings_path = self.runtime / 'settings.json'
        try:
            self.settings = json.loads(self.settings_path.read_text())
            if not isinstance(self.settings, dict): self.settings = {}
        except (OSError, ValueError):
            self.settings = {}
        self.mode = budget(self.settings.get('mode', 'standard'))['mode']
        self.started = None
        self.capture_running_settings()

    def record(self):
        record = runfile.read(self.cfg.state_dir)
        if not record or psutil is None:
            return None
        try:
            process = psutil.Process(int(record['pid']))
            command = process.cmdline()
            is_ninaivu = False
            if '-m' in command:
                idx = command.index('-m')
                is_ninaivu = idx + 1 < len(command) and command[idx+1] == 'ninaivu'
            else:
                is_ninaivu = any('ninaivu' in str(arg).lower() for arg in command)
            if not is_ninaivu or not process.is_running(): return None
            if process.create_time() > float(record.get('started_at',0)) + 2: return None
            return record
        except (ValueError, IndexError, KeyError, TypeError, _PsutilError):
            return None

    def capture_running_settings(self):
        record = self.record()
        if record and psutil is not None:
            try:
                command = psutil.Process(record['pid']).cmdline()
                if '-m' in command:
                    idx = command.index('-m')
                    self.settings['arguments'] = command[idx+2:]
                else:
                    self.settings['arguments'] = command[1:]
                self.settings['port'] = record['port']
                self.settings['admin_port'] = record['admin_port']
                if 'scheme' in record:
                    self.settings['scheme'] = record['scheme']
                for i, arg in enumerate(self.settings.get('arguments', [])):
                    if arg == '--workers' and i + 1 < len(self.settings['arguments']):
                        w = str(self.settings['arguments'][i+1])
                        if w == '1': self.mode = 'power-saving'
                        elif w in ('8', '16'): self.mode = 'performance'
                        elif w == '4': self.mode = 'standard'
            except (_PsutilError, ValueError): pass

    def save_mode(self, mode):
        if mode not in ('standard','performance','power-saving'): raise ValueError('Unknown resource mode.')
        self.mode = mode
        self.settings['mode'] = mode
        self.runtime.mkdir(parents=True,exist_ok=True)
        temporary = self.settings_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.settings,indent=2))
        temporary.replace(self.settings_path)

    def server_url(self, admin=False):
        record = self.record() or self.settings
        port = record.get('admin_port' if admin else 'port', self.cfg.admin_port if admin else self.cfg.port)
        args = self.settings.get('arguments',[])
        scheme = record.get('scheme') or ('https' if not args or any(arg=='--https' or arg=='--cert' or str(arg).startswith('--cert=') for arg in args) else 'http')
        if not args and port == 80 and not admin: port = 443
        def option(name, default):
            for i,arg in enumerate(args):
                if arg==name and i+1<len(args):return args[i+1]
                if str(arg).startswith(name+'='):return str(arg).split('=',1)[1]
            return default
        host='127.0.0.1'
        if getattr(self.cfg,'network_access',True) and '--no-mdns' not in args and '--local-only' not in args and option('--host',self.cfg.host) in ('0.0.0.0','::'):
            from ..utils.discovery import normalise_name
            family=normalise_name(option('--name','ninaivu'))
            admin_name=normalise_name(option('--admin-name',family+'-admin'))
            if not admin:
                host=family+'.local'
            elif admin_name!=family and option('--admin-host',option('--host',self.cfg.host))==option('--host',self.cfg.host):
                host=admin_name+'.local'
                port=record.get('port',443 if scheme=='https' and self.cfg.port==80 else self.cfg.port)
        suffix='' if int(port)==(443 if scheme=='https' else 80) else f':{int(port)}'
        return f'{scheme}://{host}{suffix}'

    def refresh_network_access(self):
        """Re-read the console's Network access switch, which may have been
        flipped since this panel loaded its configuration."""
        try:
            stored = json.loads((Path(self.cfg.state_dir) / 'config.json').read_text(encoding='utf-8'))
            self.cfg.network_access = bool(stored.get('network_access', True))
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def start(self):
        if self.record(): return 'Ninaivu is already running.'
        self.refresh_network_access()
        if self.started and self.started.poll() is None: return 'Ninaivu is still starting.'
        self.save_mode(self.mode)
        env = with_tool_folders(dict(os.environ, **environment(self.mode)))
        env['NINAIVU_STATE_DIR'] = str(self.cfg.state_dir)
        self._start_ollama(env)
        python = self.root / '.venv' / ('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
        if not python.is_file(): raise RuntimeError('Ninaivu’s Python environment is missing. Run the initial setup first.')
        args = self.settings.get('arguments') or ['--host',self.cfg.host,'--port',str(443 if self.cfg.port==80 else self.cfg.port),'--admin-port',str(self.cfg.admin_port),'--ai',self.cfg.ai_engine,'--https']
        env['PYTHONUNBUFFERED'] = '1'
        # Preserve the existing server's settings, replacing only the worker budget.
        clean=[];skip=False
        for arg in args:
            if skip: skip=False;continue
            if arg=='--workers': skip=True;continue
            if str(arg).startswith('--workers='): continue
            clean.append(str(arg))
        clean += ['--workers',str(budget(self.mode)['workers'])]
        # Its own session on macOS, so closing the panel — or the Terminal it
        # was opened from — leaves the server running, as it does on Windows.
        # Windows ignores start_new_session; HIDDEN is its half of this.
        with (self.runtime/'server.log').open('ab') as log:
            self.started = subprocess.Popen([str(python),'-m','ninaivu',*clean],cwd=self.root,env=env,
                                            stdout=log,stderr=log,creationflags=HIDDEN,
                                            start_new_session=True)
        for _ in range(120):
            if self.record():
                self.capture_running_settings()
                self.save_mode(self.mode)
                return 'Ninaivu is running.'
            if self.started.poll() is not None: raise RuntimeError('Ninaivu could not start. Use View logs for details.')
            time.sleep(.5)
        raise RuntimeError('Startup is taking longer than expected. Watch the status or open the logs.')

    def _start_ollama(self, env):
        if not (self.root/'.ai-models/settings.json').is_file(): return
        try:
            with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=2): return
        except OSError: pass
        executable = Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Ollama/ollama.exe'
        if not executable.is_file(): return
        env=dict(env,OLLAMA_MODELS=str(self.root/'.ai-models/ollama'),OLLAMA_HOST='127.0.0.1:11434',OLLAMA_NO_CLOUD='1')
        with (self.runtime/'ollama.log').open('ab') as log:
            subprocess.Popen([str(executable),'serve'],env=env,stdout=log,stderr=log,creationflags=HIDDEN)

    def stop(self):
        record=self.record()
        if not record:
            if self.started and self.started.poll() is None:
                raise RuntimeError('Ninaivu is still starting. Wait for Running before stopping it.')
            return 'Ninaivu is stopped.'
        self.capture_running_settings()
        self.save_mode(self.mode)
        # Reuse the existing authenticated local shutdown protocol. Never force-kill.
        from tools.stop import ask_to_stop
        args=self.settings.get('arguments',[])
        scheme = record.get('scheme') or ('https' if not args or any(arg=='--https' or arg=='--cert' or str(arg).startswith('--cert=') for arg in args) else 'http')
        server = _server_process(record)
        if not ask_to_stop(int(record['admin_port']), record.get('token', ''), 10.0, scheme):
            raise RuntimeError('Ninaivu did not accept the shutdown request. No process was forcibly stopped.')
        for _ in range(120):
            if not self.record():
                # On Windows the venv launcher can outlive the server briefly.
                # Restart must wait for that owned launcher instead of reporting
                # "still starting" and leaving the server stopped.
                if self.started and self.started.poll() is None:
                    time.sleep(.5)
                    continue
                # The run file goes before the server has let go of the state
                # folder: it still closes its announcement and services, and its
                # lock is released only when the process ends. A panel that did
                # not start this server has no handle to wait on, and a restart
                # from it started the next one in between: "Another Ninaivu server
                # is already using this state directory", and no family app.
                if not _has_ended(server):
                    time.sleep(.5)
                    continue
                self.started = None
                return 'Ninaivu stopped cleanly.'
            time.sleep(.5)
        raise RuntimeError('Ninaivu is still finishing active work. Leave it time to checkpoint; no process was forced closed.')

    def apply_mode(self, mode):
        running=bool(self.record())
        self.save_mode(mode)
        if running:
            self.stop()
            return self.start()
        return 'Mode saved for the next start.'


#: Where a Mac's package managers put the tools Ninaivu runs (ffmpeg above all).
TOOL_FOLDERS = ('/opt/homebrew/bin', '/usr/local/bin')


def with_tool_folders(env, platform=None, exists=os.path.isdir):
    """The environment with Homebrew's folders on PATH, on a Mac.

    An app opened from Finder, and anything launchd starts at sign-in, is given
    /usr/bin:/bin:/usr/sbin:/sbin and nothing else. A server started from there
    found no ffmpeg: no video converted for the browser, no poster frames, and
    sound files drawn without the shape of their sound.
    """
    if (platform or sys.platform) != 'darwin':
        return env
    parts = [p for p in env.get('PATH', '').split(os.pathsep) if p]
    for folder in TOOL_FOLDERS:
        if folder not in parts and exists(folder):
            parts.append(folder)
    return {**env, 'PATH': os.pathsep.join(parts)}


def _server_process(record):
    """The running server's process, taken while its run file still names it."""
    if psutil is None:
        return None
    try:
        return psutil.Process(int(record['pid']))
    except (ValueError, KeyError, TypeError, _PsutilError):
        return None


def _has_ended(process):
    """Whether it has exited. One exited but not yet collected by whatever
    started it holds no files, and so no lock, any more."""
    if process is None:
        return True
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except _PsutilError:
        return True


class Monitor:
    def __init__(self, controller):
        self.controller=controller
        self.previous={}
        self.last=time.monotonic()

    def sample(self):
        record=self.controller.record()
        if psutil is None:
            return {'running':bool(record),'cpu':0.0, 'ram_percent':0.0,
                    'ram_used':0, 'ram_total':0, 'server_cpu':0.0,
                    'server_ram':0,'threads':0,'battery':None,
                    'plugged':None,'disk_free':0,
                    'uptime':max(0,time.time()-record.get('started_at',time.time())) if record else 0}
        now=time.monotonic();elapsed=max(.01,now-self.last);self.last=now
        processes=[]
        if record:
            try:
                parent=psutil.Process(record['pid']);processes=[parent,*parent.children(recursive=True)]
            except _PsutilError: pass
        cpu=rss=threads=0;next_times={}
        for process in processes:
            try:
                with process.oneshot():
                    ident=(process.pid,process.create_time());times=process.cpu_times();total=times.user+times.system
                    if ident in self.previous: cpu+=max(0,total-self.previous[ident])/elapsed*100
                    next_times[ident]=total;rss+=process.memory_info().rss;threads+=process.num_threads()
            except _PsutilError: continue
        self.previous=next_times
        memory=psutil.virtual_memory();battery=psutil.sensors_battery()
        disk=psutil.disk_usage(str(self.controller.root))
        return {'running':bool(record),'cpu':psutil.cpu_percent(), 'ram_percent':memory.percent,
                'ram_used':memory.used, 'ram_total':memory.total, 'server_cpu':min(100,cpu/(psutil.cpu_count() or 1)),
                'server_ram':rss,'threads':threads,'battery':battery.percent if battery else None,
                'plugged':battery.power_plugged if battery else None,'disk_free':disk.free,
                'uptime':max(0,time.time()-record.get('started_at',time.time())) if record else 0}
