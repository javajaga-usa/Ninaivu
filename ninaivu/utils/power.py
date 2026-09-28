"""Adaptive process power policy for Ninaivu.

Serving an already-indexed album is mostly short, bursty work and should stay
on the efficient cores of a machine that may run all day.  An archive run is
the opposite: it is explicit, finite work where finishing sooner lets the
drives and CPU return to idle sooner.

Windows exposes exactly that distinction as process power throttling (EcoQoS).
Ninaivu enables it while serving and temporarily disables it while the archive
engine is active.  The call is advisory, requires no administrator rights, and
is deliberately best-effort.  Other platforms remain unmanaged because their
nearest process-wide controls (notably ``nice``) cannot always be reversed by
an unprivileged process.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

__all__ = ["PowerPolicy"]

# PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
PROCESS_POWER_THROTTLING = 4
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1


def _set_windows_execution_speed_throttled(throttled: bool) -> bool:
    """Set Windows EcoQoS for this process; return whether it was accepted."""
    try:
        import ctypes

        class PowerThrottlingState(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        state = PowerThrottlingState(
            PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED if throttled else 0,
        )
        kernel32 = ctypes.windll.kernel32
        get_process = kernel32.GetCurrentProcess
        get_process.argtypes = []
        get_process.restype = ctypes.c_void_p
        set_information = kernel32.SetProcessInformation
        set_information.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
        ]
        set_information.restype = ctypes.c_int
        return bool(set_information(
            get_process(), PROCESS_POWER_THROTTLING,
            ctypes.byref(state), ctypes.sizeof(state),
        ))
    except Exception:  # noqa: BLE001 - optional OS hint, never app-critical
        return False


class PowerPolicy:
    """Switch between efficient serving and finite archive performance work.

    Archive activity is reference-counted so an extra cleanup callback cannot
    return the process to EcoQoS while another caller still needs performance.
    In normal operation the archive engine permits only one job, but keeping
    that invariant here makes failure cleanup and tests safely idempotent.
    """

    def __init__(self, profile: str = 'adaptive') -> None:
        self._lock = threading.Lock()
        self._started = False
        self._archive_depth = 0
        self.mode = "unmanaged"
        self.managed = False
        self.profile = profile

    def _idle_mode(self):
        return {'standard':'standard', 'performance':'performance', 'power-saving':'efficient'}.get(self.profile, 'efficient')

    def _apply(self, mode: str) -> bool:
        if sys.platform != "win32":
            self.mode = "unmanaged"
            self.managed = False
            return False
        accepted = _set_windows_execution_speed_throttled(mode == "efficient")
        self.mode = mode if accepted else "unmanaged"
        self.managed = accepted
        return accepted

    def start(self) -> bool:
        """Enter the all-day, efficient serving policy."""
        with self._lock:
            if self._started:
                return self.managed
            self._started = True
            return self._apply(self._idle_mode())

    def archive_started(self) -> bool:
        """Request full execution speed until the matching finish call."""
        with self._lock:
            self._started = True
            self._archive_depth += 1
            if self._archive_depth > 1 and self.mode == "performance":
                return self.managed
            return self._apply('efficient' if self.profile == 'power-saving' else 'performance')

    def archive_finished(self) -> bool:
        """Return to efficient serving after the last archive run ends."""
        with self._lock:
            self._archive_depth = max(0, self._archive_depth - 1)
            if self._archive_depth or not self._started:
                return self.managed
            return self._apply(self._idle_mode())

    def stop(self) -> None:
        """Remove Ninaivu's hint and restore normal Windows scheduling."""
        with self._lock:
            if self._started and sys.platform == "win32":
                _set_windows_execution_speed_throttled(False)
            self._started = False
            self._archive_depth = 0
            self.mode = "unmanaged"
            self.managed = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                "managed": self.managed,
                "archive_active": self._archive_depth > 0,
            }
