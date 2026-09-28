#!/usr/bin/env python3
"""Verify a Ninaivu archive using only its portable recovery report.

The verifier itself lives at ninaivu/archive/verify_archive.py, because Ninaivu
copies it onto every archive it writes and so it has to ship with the package.
This keeps the old command working:

    python tools/verify_recovery_report.py ninaivu-recovery-20260914.json [archive]

It loads that file by path rather than importing Ninaivu, so, like the verifier,
it needs nothing but the standard library.
"""

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "ninaivu" / "archive" / "verify_archive.py"
_spec = importlib.util.spec_from_file_location("ninaivu_verify_archive", _PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

file_hash = _module.file_hash
verify = _module.verify
main = _module.main

if __name__ == "__main__":
    sys.exit(main())
