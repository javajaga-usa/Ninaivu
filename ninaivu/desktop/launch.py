"""Guard the packaged desktop entry point, including imports before main."""
from __future__ import annotations

import sys


def main(argv=None):
    try:
        from .app import main as panel
        return panel(argv)
    except Exception as exc:  # noqa: BLE001 — show errors from packaged imports too
        from .errors import tell
        tell(f'Could not open Ninaivu: {exc}\n\nPlease reinstall Ninaivu and try again.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
