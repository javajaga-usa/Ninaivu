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


def _linux_battery() -> tuple[bool | None, int | None]:
    supplies = Path('/sys/class/power_supply')
    try:
        batteries = [p for p in supplies.iterdir()
                     if (p / 'type').read_text(errors='ignore').strip() == 'Battery']
    except OSError:
        return None, None
    if not batteries:
        return False, None
    battery = batteries[0]
    try:
        status = (battery / 'status').read_text(errors='ignore').strip().lower()
        percent = int((battery / 'capacity').read_text().strip())
        return status not in {'charging', 'full', 'not charging'}, percent
    except (OSError, ValueError):
        return None, None


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
