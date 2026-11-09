"""Write tests/data/route_manifest.txt: every route, who may call it, and
whether it changes anything (ninaivu/server/routes.py).

    python tools/route_manifest.py          # write it
    python tools/route_manifest.py --check  # exit 1 if it is out of date
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FILE = ROOT / "tests" / "data" / "route_manifest.txt"


def build() -> str:
    """The manifest of the family app and the console, as a fresh install makes them."""
    logging.disable(logging.WARNING)
    # Windows will not delete a file something still has open; whatever is
    # left behind in a temporary folder is the system's to clear.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as state:
        os.environ["NINAIVU_STATE_DIR"] = state
        from ninaivu import build_services, create_admin_app, create_home_app
        from ninaivu.server import routes
        from ninaivu.server.config import Config
        cfg = Config.load()
        cfg.watch = False
        cfg.ensure_dirs()
        services = build_services(cfg)
        try:
            found = routes.manifest([("home", create_home_app(services)),
                                     ("admin", create_admin_app(services))])
        finally:
            from ninaivu.archive import database as archive_db
            from ninaivu.storage import db
            db.close_all()
            archive_db.close_db()
        return routes.render(found)


def main(argv: list[str]) -> int:
    text = build()
    if "--check" in argv:
        current = FILE.read_text(encoding="utf-8") if FILE.exists() else ""
        if current != text:
            print(f"{FILE.relative_to(ROOT)} is out of date: run python tools/route_manifest.py")
            return 1
        return 0
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {FILE.relative_to(ROOT)} ({text.count(chr(10)) - 2} routes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
