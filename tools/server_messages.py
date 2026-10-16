"""Collect the sentences the server can send to a person, for translation.

    python tools/server_messages.py          rewrite ninaivu/messages.py
    python tools/server_messages.py --check  exit 1 if it is out of date

The family app and the console show what the server says when something is
refused or goes wrong: an ``{"error": …}`` answer, an ``abort(…,
description=…)``, the message of an exception a route passes on, and the
templates of :class:`ninaivu.words.Said` and ``refusal``. The pages look each
one up with ``i18n.fromServer`` — so a Tamil reader is told in Tamil — and the
locale guard (tests/test_language.py) has to know the sentences exist, which
is what ``ninaivu/messages.py`` is: every one of them, marked with ``said``.

It is written by this script rather than by hand, and a test fails when it no
longer matches the code, so a new message cannot slip past untranslated.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ninaivu" / "messages.py"

#: Keys whose value a page shows to a person.
SHOWN = {"error", "message", "why", "reason", "hint", "problem"}
#: Exceptions whose message a route hands on as its answer.
RAISED = {"ValueError", "RuntimeError", "PermissionError", "LookupError", "OSError",
          "FileNotFoundError", "TimeoutError", "ConnectionError"}
#: Not seen in a browser: the desktop Control Panel and the command line are
#: in English, and the generated catalogue itself.
SKIP = ("ninaivu/desktop/", "ninaivu/cli/", "ninaivu/__main__.py", "ninaivu/messages.py",
        "ninaivu/words.py")


def sources() -> list[Path]:
    found = [*(ROOT / "ninaivu").rglob("*.py"), *(ROOT / "extensions").rglob("*.py")]
    return sorted(p for p in found
                  if not any(p.relative_to(ROOT).as_posix().startswith(s) for s in SKIP))


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _text(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def is_sentence(text: str) -> bool:
    """Words a person reads, not a code ("no-embedding") or a blank."""
    return " " in text.strip() and any(c.isalpha() for c in text)


def collect() -> list[str]:
    found: dict[str, None] = {}
    for path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            texts: list[str | None] = []
            if isinstance(node, ast.Dict):
                texts += [_text(v) for k, v in zip(node.keys, node.values)
                          if _text(k) in SHOWN]
            elif isinstance(node, ast.Call):
                name = _name(node.func)
                if name == "abort":
                    texts += [_text(kw.value) for kw in node.keywords if kw.arg == "description"]
                    if len(node.args) > 1:
                        texts.append(_text(node.args[1]))
                elif name in ("Said", "refusal") and node.args:
                    texts.append(_text(node.args[0]))
            elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                    and _name(node.exc.func) in RAISED and node.exc.args:
                texts.append(_text(node.exc.args[0]))
            for text in texts:
                if text and is_sentence(text):
                    found[text] = None
    return sorted(found)


def render(messages: list[str]) -> str:
    lines = [
        '"""Every sentence the server can send to a person — written by',
        "tools/server_messages.py, not by hand.",
        "",
        "Marked with ``said`` so the locale guard knows the pages translate them",
        "(``i18n.fromServer``); see ninaivu/words.py.",
        '"""',
        "# ruff: noqa: E501",
        "from __future__ import annotations",
        "",
        "from .words import said",
        "",
        "MESSAGES = (",
    ]
    for text in messages:
        lines.append(f"    said({text!r}),")
    lines.append(")")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    wanted = render(collect())
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != wanted:
            print("ninaivu/messages.py is out of date: run python tools/server_messages.py")
            return 1
        return 0
    OUT.write_text(wanted, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(collect())} sentences")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
