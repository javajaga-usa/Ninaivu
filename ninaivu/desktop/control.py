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
from ..media import model_catalog
from ..server.config import Config
from ..server import runfile
from ..utils.resources import budget, environment



#: Where an installed copy keeps its run-time files. Deliberately not
#: NINAIVU_ROOT: the server reads that as the *library* folder, and an
#: installer that set it to its own folder made the server index Python's
#: bundled icons as the family's photographs.
HOME_VAR = 'NINAIVU_HOME'


def ninaivu_root() -> Path:
    """Where Ninaivu keeps the files beside itself — the run-time folder, the
    server log, the .venv when there is one.

    A checkout: the repository root, two levels up from this file. An
    installed package (the Windows installer, the macOS app): whatever the
    installer put in ``NINAIVU_HOME``, since two levels up from a package in
    site-packages is nowhere a person can look.
    """
    told = os.environ.get('NINAIVU_HOME')
    if told:
        return Path(told).expanduser()
    return Path(__file__).resolve().parents[2]


_CONTROL_DIRS: dict[Path, Path] = {}


def _user_control_dir(platform: str | None = None) -> Path:
    """A per-user folder for the run-time files, for when the installation
    itself cannot be written to."""
    platform = platform or sys.platform
    if platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
        return base / 'Ninaivu' / 'control'
    if platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Ninaivu' / 'control'
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return base / 'ninaivu' / 'control'


def _writable(folder: Path) -> bool:
    """Whether *folder* can be made and written to. Tried rather than asked:
    os.access on Windows looks only at the read-only flag, not at the
    permissions that keep a person out of Program Files."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f'.probe-{os.getpid()}'
        probe.write_bytes(b'')
        probe.unlink()
        return True
    except OSError:
        return False


#: A run-time log larger than this is moved aside (to ``<name>.1``) when it is
#: next opened. They were appended to for ever, and they hold the addresses
#: the server answered on and the first-run setup code.
LOG_LIMIT = 5 * 1024 * 1024


def private_folder(folder: Path) -> Path:
    """Make *folder*, readable by this account alone where the system has modes.

    The logs in it name every address the server answered on and the setup
    code shown at first run — not for other accounts on a shared computer. A
    folder made before this, at 0755, is tightened when it is ours.
    """
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix':
        try:
            st = folder.stat()
            if st.st_uid == os.getuid() and st.st_mode & 0o077:
                os.chmod(folder, 0o700)
        except OSError:
            pass
    return folder


def trim_log(path: Path, limit: int | None = None, rotate: bool = True) -> None:
    """Keep *path* under *limit*: moved to ``.1`` (replacing the one before),
    or emptied where it cannot be moved — on Windows while another process
    still has it open, or for a log another program writes to (*rotate* off),
    where a moved file would go on growing under its new name."""
    try:
        if path.stat().st_size <= (LOG_LIMIT if limit is None else limit):
            return
    except OSError:
        return
    if rotate:
        try:
            os.replace(path, path.with_name(path.name + '.1'))
            return
        except OSError:
            pass
    try:
        os.truncate(path, 0)
    except OSError:
        pass


def open_log(path: Path):
    """*path* opened to append to, owner-only, and trimmed first if it has grown
    past :data:`LOG_LIMIT`. Binary, for handing to a child process."""
    private_folder(path.parent)
    trim_log(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_BINARY', 0),
                         0o600)
    if os.name == 'posix':
        try:
            os.fchmod(descriptor, 0o600)        # one made before, at the umask's 0644
        except OSError:
            pass
    return os.fdopen(descriptor, 'ab')


def control_dir(root: Path | None = None, platform: str | None = None) -> Path:
    """The run-time folder: settings.json, server.log, restart.log and the rest.

    ``.ninaivu-control`` beside the checkout or installation when that can be
    written to, as it always has been. An installation under Program Files
    cannot be by a person without administrator rights, so there it is a
    per-user folder instead (``%LOCALAPPDATA%\\Ninaivu\\control`` on Windows).
    """
    root = Path(root or ninaivu_root())
    if root not in _CONTROL_DIRS:
        beside = root / '.ninaivu-control'
        chosen = beside if _writable(beside) else _user_control_dir(platform)
        try:
            private_folder(chosen)
        except OSError:
            pass                # made when something is first written to it
        _CONTROL_DIRS[root] = chosen
    return _CONTROL_DIRS[root]


def python_for_server(root: Path, platform: str | None = None) -> Path:
    """The interpreter that runs the server: the checkout's ``.venv``, or the
    one this process runs on (an installer bundles one and runs the tray on
    it), or ``NINAIVU_PYTHON`` when the installer says otherwise."""
    told = os.environ.get('NINAIVU_PYTHON')
    if told:
        return Path(told)
    platform = platform or sys.platform
    venv = root / '.venv' / ('Scripts/python.exe' if platform == 'win32' else 'bin/python')
    if venv.is_file() or not os.environ.get('NINAIVU_HOME'):
        # A checkout: its .venv, present or (the setup not run yet) expected.
        return venv
    return Path(sys.executable)


ROOT = ninaivu_root()
HIDDEN = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


#: Options that belong to one start and must not be carried into the next.
#: The panel remembers the arguments the running server was started with and
#: starts the next one with them; these would do harm there:
#:   --admin USER:PASSWORD  creates the first administrator. Again, it fails
#:                          (the account exists) and the server exits; and the
#:                          password was kept, in plain text, in settings.json.
#:   --rescan               a full re-index on every restart.
#:   --open                 a browser window on every restart.
#:   --supervised           a server the panel starts has no service manager.
#: The value each takes, if any, goes with it.
ONE_SHOT_OPTIONS = {'--admin': True, '--rescan': False, '--open': False,
                    '--supervised': False}


def without_one_shot_options(arguments):
    """*arguments* without the ONE_SHOT_OPTIONS, as ``--flag value`` or
    ``--flag=value``. Everything else, in order, as strings."""
    kept, skip = [], False
    for arg in arguments or []:
        arg = str(arg)
        if skip:
            skip = False
            continue
        name = arg.split('=', 1)[0]
        if name in ONE_SHOT_OPTIONS:
            # Only the separate form takes the next word with it.
            skip = ONE_SHOT_OPTIONS[name] and '=' not in arg
            continue
        kept.append(arg)
    return kept


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
    def __init__(self, root=None, cfg=None):
        self.root = Path(root or ninaivu_root())
        self.cfg = cfg or Config.load()
        self.runtime = control_dir(self.root)
        self.settings_path = self.runtime / 'settings.json'
        try:
            self.settings = json.loads(self.settings_path.read_text())
            if not isinstance(self.settings, dict): self.settings = {}
        except (OSError, ValueError):
            self.settings = {}
        # A settings file written before the one-shot options were left out
        # may still hold them - a password among them. The next save rewrites
        # it without.
        if 'arguments' in self.settings:
            self.settings['arguments'] = without_one_shot_options(self.settings['arguments'])
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
                process = psutil.Process(record['pid'])
                command = process.cmdline()
                if '-m' in command:
                    idx = command.index('-m')
                    arguments = command[idx+2:]
                else:
                    arguments = command[1:]
                self.settings['arguments'] = without_one_shot_options(arguments)
                self.settings['port'] = record['port']
                self.settings['admin_port'] = record['admin_port']
                if 'scheme' in record:
                    self.settings['scheme'] = record['scheme']
                self.mode = running_mode(process, self.settings['arguments'], self.mode)
            except (_PsutilError, ValueError): pass

    def save_mode(self, mode):
        if mode not in ('standard','performance','power-saving'): raise ValueError('Unknown resource mode.')
        self.mode = mode
        self.save_setting('mode', mode)

    def save_setting(self, name, value):
        """Keep one of the panel's settings (the mode, the look) in settings.json."""
        self.settings[name] = value
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
            elif admin_name!=family and (
                    option('--admin-host',None)==option('--host',self.cfg.host)
                    # Unset: the console is on the network only when the
                    # household opened it there (Server page); otherwise it
                    # answers on this computer, which is where the tray is.
                    or (option('--admin-host',None) is None and getattr(self.cfg,'console_on_network',False))):
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
        python = python_for_server(self.root)
        if not python.is_file(): raise RuntimeError('Ninaivu’s Python environment is missing. Run the initial setup first.')
        args = without_one_shot_options(self.settings.get('arguments')) or ['--host',self.cfg.host,'--port',str(443 if self.cfg.port==80 else self.cfg.port),'--admin-port',str(self.cfg.admin_port),'--ai',self.cfg.ai_engine,'--https']
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
        with open_log(self.runtime/'server.log') as log:
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
        if not model_catalog.settings_path().is_file(): return
        try:
            with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=2): return
        except OSError: pass
        executable = Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Ollama/ollama.exe'
        if not executable.is_file(): return
        env=dict(env,OLLAMA_MODELS=str(model_catalog.models_root()/'ollama'),OLLAMA_HOST='127.0.0.1:11434',OLLAMA_NO_CLOUD='1')
        with open_log(self.runtime/'ollama.log') as log:
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
        from ..server.stop import ask_to_stop
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


def running_mode(process, arguments, current='standard'):
    """The resource mode a running server was started in.

    The mode it was given is in its environment (``NINAIVU_RESOURCE_MODE``),
    which is the answer whenever this account may read it. Otherwise it is
    worked out from ``--workers``, against what each mode gives this
    computer: the numbers used to be fixed (1, 4, 8), and on a machine with
    sixteen cores standard's eight workers read as performance, so the
    control panel ticked the wrong mode and saved it for the next start.
    *current* wins a tie, and stays when nothing can be told.
    """
    try:
        told = (process.environ() or {}).get('NINAIVU_RESOURCE_MODE') if process else None
    except (_PsutilError, OSError, AttributeError):
        told = None
    if told in ('standard', 'performance', 'power-saving'):
        return told
    workers = None
    arguments = [str(arg) for arg in arguments or []]
    for i, arg in enumerate(arguments):
        if arg == '--workers' and i + 1 < len(arguments):
            workers = arguments[i + 1]
        elif arg.startswith('--workers='):
            workers = arg.split('=', 1)[1]
    if workers is None:
        return current
    for mode in (current, 'standard', 'performance', 'power-saving'):
        if str(budget(mode)['workers']) == workers:
            return budget(mode)['mode']
    return current


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
    # A Mac's separator, not this computer's: *platform* says whose PATH it is.
    parts = [p for p in env.get('PATH', '').split(':') if p]
    for folder in TOOL_FOLDERS:
        if folder not in parts and exists(folder):
            parts.append(folder)
    return {**env, 'PATH': ':'.join(parts)}


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
        # Each reading on its own: a machine without a battery sensor, a
        # sandbox that hides memory figures or a drive that went away answers
        # one of these with an error, and the panel's other numbers still count.
        memory=_reading(psutil.virtual_memory);battery=_reading(psutil.sensors_battery)
        disk=_reading(lambda: psutil.disk_usage(str(self.controller.root)))
        return {'running':bool(record),'cpu':_reading(psutil.cpu_percent,0.0),
                'ram_percent':getattr(memory,'percent',0.0),
                'ram_used':getattr(memory,'used',0), 'ram_total':getattr(memory,'total',0),
                'server_cpu':min(100,cpu/(_reading(psutil.cpu_count) or 1)),
                'server_ram':rss,'threads':threads,'battery':getattr(battery,'percent',None),
                'plugged':getattr(battery,'power_plugged',None),'disk_free':getattr(disk,'free',0),
                'uptime':max(0,time.time()-record.get('started_at',time.time())) if record else 0}


def _reading(read, default=None):
    """One of psutil's system figures, or *default* where it cannot be read."""
    try:
        return read()
    except (OSError, RuntimeError, NotImplementedError, AttributeError, _PsutilError):
        return default


def main(argv=None) -> int:
    """For installers and scripts: ``python -m ninaivu.desktop.control --stop``
    before an upgrade or an uninstall (Windows cannot replace a program that
    is running), and ``--status``. Stops the way the panel does: by asking,
    never by force. From Ninaivu Lite."""
    import argparse
    parser = argparse.ArgumentParser(prog='python -m ninaivu.desktop.control')
    parser.add_argument('--stop', action='store_true',
                        help='ask a running Ninaivu to stop, and wait for it')
    parser.add_argument('--status', action='store_true',
                        help='say whether Ninaivu is running, and where')
    args = parser.parse_args(argv)
    if not (args.stop or args.status):
        parser.print_help()
        return 2
    controller = Controller()
    if args.stop:
        if psutil is None and runfile.read(controller.cfg.state_dir):
            # Without psutil the panel cannot tell a live run file from a stale
            # one; asking costs nothing, and a server that is not there says no.
            from ..server.stop import ask_to_stop
            record = runfile.read(controller.cfg.state_dir) or {}
            scheme = record.get('scheme') or 'http'
            try:
                stopped = ask_to_stop(int(record['admin_port']), record.get('token', ''),
                                      10.0, scheme)
            except (KeyError, TypeError, ValueError):
                stopped = False
            print('Ninaivu was asked to stop.' if stopped else 'Ninaivu is stopped.')
        else:
            try:
                print(controller.stop())
            except RuntimeError as exc:
                print(exc, file=sys.stderr)
                return 1
    if args.status:
        record = controller.record()
        print(f"running at {controller.server_url()}" if record else 'stopped')
    return 0


if __name__ == '__main__':
    sys.exit(main())
