"""Kept for a checkout's habits: the command lives in the package now,
``ninaivu/cli/backup_restore.py`` — run it as ``python -m ninaivu backup|restore|list-backups …``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ninaivu.cli import backup_restore as _command  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_command.main())
sys.modules[__name__] = _command
