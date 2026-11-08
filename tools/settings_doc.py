"""Write docs/settings.md from the settings' declarations.

    python tools/settings_doc.py          # write it
    python tools/settings_doc.py --check  # exit 1 if it is out of date
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ninaivu.server.settings_groups import reference  # noqa: E402

PAGE = ROOT / "docs" / "settings.md"


def main(argv: list[str]) -> int:
    text = reference() + "\n"
    if "--check" in argv:
        current = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""
        if current != text:
            print(f"{PAGE.relative_to(ROOT)} is out of date: run python tools/settings_doc.py")
            return 1
        return 0
    PAGE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {PAGE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
