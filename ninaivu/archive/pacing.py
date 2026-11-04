"""Battery and thermal guardrails for long archive operations."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class SystemConditions:
    on_battery: bool | None = None
    battery_percent: int | None = None
    temperature_c: float | None = None


def _windows_battery() -> tuple[bool | None, int | None]:
    try:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        status = Status()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None, None
        on_battery = None if status.ACLineStatus == 255 else status.ACLineStatus == 0
        percent = None if status.BatteryLifePercent == 255 else int(status.BatteryLifePercent)
        return on_battery, percent
    except Exception:  # noqa: BLE001 - optional advisory signal
        return None, None


#: Power supplies that feed the computer itself. A USB-C port shows up as one
#: of the USB kinds and is online while a charger is plugged into it.
_LINUX_MAINS = {'mains', 'usb', 'usb_c', 'usb_pd', 'usb_pd_drp', 'usb_dcp', 'usb_cdp', 'usb_aca'}


def _sysfs(path: Path) -> str:
    try:
        return path.read_text(errors='ignore').strip()
    except OSError:
        return ''


def _linux_battery(supplies: Path = Path('/sys/class/power_supply')
                   ) -> tuple[bool | None, int | None]:
    """(on battery, percent) of the computer's own battery.

    ``/sys/class/power_supply`` lists every battery the kernel knows, and that
    includes a wireless mouse, keyboard, headset or game controller (their
    ``scope`` is ``Device``). Reading whichever came first could report the
    mouse: a laptop at 96% read as 0%, and a consolidation paused for a
    "low battery" while the Control Panel said 96%. So only the computer's
    own batteries count (``scope`` absent or ``System``), the level is all of
    them together, and "on battery" means no charger is online, whenever the
    computer says either way.
    """
    try:
        entries = sorted(supplies.iterdir())
    except OSError:
        return None, None
    batteries: list[Path] = []
    mains: list[bool] = []
    for entry in entries:
        if _sysfs(entry / 'scope').lower() == 'device':
            continue                    # a mouse's, a headset's: not this computer's
        kind = _sysfs(entry / 'type').lower()
        if kind == 'battery':
            if _sysfs(entry / 'present') != '0':
                batteries.append(entry)
        elif kind in _LINUX_MAINS:
            online = _sysfs(entry / 'online')
            if online in {'0', '1'}:
                mains.append(online == '1')
    if not batteries:
        return False, None
    percent = _linux_percent(batteries)
    if mains:
        return not any(mains), percent
    statuses = [_sysfs(b / 'status').lower() for b in batteries]
    if any(statuses):
        return any(s == 'discharging' for s in statuses), percent
    return None, percent


def _linux_percent(batteries: list[Path]) -> int | None:
    """The charge left in *batteries* together: by energy when every one says
    it (two batteries of different sizes), by their mean level otherwise."""
    for now_name, full_name in (('energy_now', 'energy_full'), ('charge_now', 'charge_full')):
        try:
            now = sum(int(_sysfs(b / now_name)) for b in batteries)
            full = sum(int(_sysfs(b / full_name)) for b in batteries)
        except ValueError:
            continue
        if full > 0:
            return max(0, min(100, round(100 * now / full)))
    levels = []
    for battery in batteries:
        try:
            levels.append(int(_sysfs(battery / 'capacity')))
        except ValueError:
            continue
    return max(0, min(100, round(sum(levels) / len(levels)))) if levels else None


def _macos_battery() -> tuple[bool | None, int | None]:
    try:
        result = subprocess.run(['pmset', '-g', 'batt'], capture_output=True,
                                text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, None
    text = result.stdout or ''
    match = re.search(r'(\d+)%', text)
    percent = int(match.group(1)) if match else None
    return ('Battery Power' in text), percent


def _temperature() -> float | None:
    # psutil gives the most portable answer where it is already installed.
    try:
        import psutil
        groups = psutil.sensors_temperatures(fahrenheit=False)
        values = [float(item.current) for items in groups.values() for item in items
                  if item.current is not None and 0 < float(item.current) < 150]
        if values:
            return max(values)
    except (ImportError, AttributeError, OSError, ValueError, NotImplementedError,
            RuntimeError):
        pass

    # A dependency-free Linux fallback.
    if sys.platform.startswith('linux'):
        values = []
        try:
            for path in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
                raw = float(path.read_text().strip())
                value = raw / 1000 if raw > 1000 else raw
                if 0 < value < 150:
                    values.append(value)
        except (OSError, ValueError):
            pass
        if values:
            return max(values)
    return None


def read_battery() -> tuple[bool | None, int | None]:
    """(on battery, percent), each None when this machine cannot say."""
    if sys.platform == 'win32':
        return _windows_battery()
    if sys.platform == 'darwin':
        return _macos_battery()
    return _linux_battery()


def read_system_conditions() -> SystemConditions:
    battery, percent = read_battery()
    return SystemConditions(battery, percent, _temperature())


#: Pause owed for each MiB read while throttled.
THROTTLE_SECONDS_PER_MIB = 0.05
#: Smallest pause worth taking. Owed time below this accumulates instead.
_SMALLEST_PAUSE = 0.05


class ArchivePacer:
    """Keep long I/O safe on battery and under thermal pressure.

    Conditions are sampled at most every five seconds. On battery or a warm
    CPU, reads are slowed by 50 ms for every MiB actually read, which reduces
    sustained load without turning the job into a crawl. A nearly empty battery
    or critical temperature parks the job until AC returns or the machine
    cools; Stop remains responsive.

    The pause is charged per byte, not per read. It used to be a flat 50 ms
    before every read, and a photo takes several: the copy, the read that finds
    the end of the file, and the verifying re-read. A small photo paid a quarter
    of a second whatever its size -- measured at 53 s on battery against 1-2 s
    on mains for 200 photos, and about seven hours of pure waiting for a
    100,000-photo library. Large files are throttled exactly as before; small
    ones now pay for the bytes they are, with short debts pooled into one pause.
    """

    def __init__(self, reader: Callable[[], SystemConditions] = read_system_conditions,
                 sleeper: Callable[[float], None] = time.sleep,
                 sample_interval: float = 5.0) -> None:
        self.reader = reader
        self.sleeper = sleeper
        self.sample_interval = sample_interval
        self._sampled_at = 0.0
        self.conditions = SystemConditions()
        self.mode = 'full-speed'
        self.reason = ''
        self._owed = 0.0

    def _sample(self, force: bool = False) -> SystemConditions:
        now = time.monotonic()
        if force or now - self._sampled_at >= self.sample_interval:
            try:
                self.conditions = self.reader()
            except Exception:  # noqa: BLE001 - sensors must never break a copy
                self.conditions = SystemConditions()
            self._sampled_at = now
        return self.conditions

    @staticmethod
    def _decision(c: SystemConditions) -> tuple[str, str]:
        if c.temperature_c is not None and c.temperature_c >= 90:
            return 'paused', f'machine is too hot ({c.temperature_c:.0f}°C)'
        if c.on_battery and c.battery_percent is not None and c.battery_percent <= 15:
            return 'paused', f'battery is low ({c.battery_percent}%)'
        if c.temperature_c is not None and c.temperature_c >= 80:
            return 'throttled', f'machine is warm ({c.temperature_c:.0f}°C)'
        if c.on_battery:
            suffix = f' ({c.battery_percent}%)' if c.battery_percent is not None else ''
            return 'throttled', 'running on battery' + suffix
        return 'full-speed', ''

    def checkpoint(self, gate, nbytes: int | None = None) -> None:
        """Call before each read, passing how many bytes the previous read got.

        Parks here while the machine is too hot or the battery too low, before
        anything more is read. While throttled, charges the pause those bytes
        owe. ``nbytes=None`` charges one whole chunk, for callers that do not
        count.
        """
        while True:
            gate.check()
            self.mode, self.reason = self._decision(self._sample(force=self.mode == 'paused'))
            if self.mode != 'paused':
                break
            self.sleeper(2.0)
        if self.mode != 'throttled':
            self._owed = 0.0
            return
        if nbytes is None:
            self._owed += THROTTLE_SECONDS_PER_MIB
        else:
            self._owed += THROTTLE_SECONDS_PER_MIB * max(0, nbytes) / (1 << 20)
        if self._owed >= _SMALLEST_PAUSE:
            owed, self._owed = self._owed, 0.0
            self.sleeper(owed)

    def snapshot(self) -> dict:
        c = self.conditions
        return {
            'mode': self.mode,
            'reason': self.reason,
            'on_battery': c.on_battery,
            'battery_percent': c.battery_percent,
            'temperature_c': c.temperature_c,
        }
