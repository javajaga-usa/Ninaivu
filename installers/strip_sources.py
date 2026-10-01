"""Rewrite wheels so they carry bytecode and no Python source.

    python installers/strip_sources.py WHEEL [WHEEL ...]

Every installer bundles Ninaivu and its two extensions as wheels. A wheel is a
zip of the package as written, ``.py`` files and all; what goes out to other
people should not be. Each ``.py`` in the wheel is compiled to a ``.pyc`` that
sits where the source was (the legacy, sourceless layout Python has always
imported from), the source is dropped, and the wheel's RECORD is rewritten
for what is now in it, so pip installs it as any other wheel.

The bytecode is tied to the Python that compiles it: a ``.pyc`` from 3.12
loads only on 3.12. Every build runs this with the same Python it bundles, so
the installers are consistent by construction; a mismatch fails at import,
not quietly. The web files under ``static/`` and ``templates/`` are left as
they are — a browser needs them as text, and they are not Python.
"""
from __future__ import annotations

import base64
import hashlib
import py_compile
import sys
import tempfile
import zipfile
from pathlib import Path


def strip(wheel: Path, optimize: int = 0) -> tuple[int, int]:
    """Replace *wheel* in place with its sourceless twin; returns (compiled, kept)."""
    compiled = kept = 0
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / wheel.name
        with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            record_name = next(n for n in src.namelist() if n.endswith(".dist-info/RECORD"))
            records: list[str] = []
            for info in src.infolist():
                name = info.filename
                if name == record_name or name.endswith("/"):
                    continue
                data = src.read(name)
                if name.endswith(".py"):
                    source = Path(scratch) / "source.py"
                    source.write_bytes(data)
                    target = Path(scratch) / "compiled.pyc"
                    # cfile keeps the module's real path in the bytecode, so a
                    # traceback names ninaivu/… and not a scratch folder.
                    py_compile.compile(str(source), cfile=str(target), dfile=name, doraise=True, optimize=optimize)
                    data = target.read_bytes()
                    name = name[:-3] + ".pyc"
                    compiled += 1
                else:
                    kept += 1
                dst.writestr(zipfile.ZipInfo(name, date_time=info.date_time), data, zipfile.ZIP_DEFLATED)
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                records.append(f"{name},sha256={digest},{len(data)}")
            records.append(f"{record_name},,")
            dst.writestr(record_name, "\n".join(records) + "\n")
        out.replace(wheel)
    return compiled, kept


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        wheel = Path(arg)
        compiled, kept = strip(wheel)
        with zipfile.ZipFile(wheel) as check:
            left = [n for n in check.namelist() if n.endswith(".py")]
        if left:
            raise SystemExit(f"{wheel.name}: source still inside: {left[:3]}")
        print(f"{wheel.name}: {compiled} modules compiled, {kept} other files kept, no source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
