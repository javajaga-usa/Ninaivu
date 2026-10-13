"""The page's JavaScript modules, each at a URL that changes when it does.

The entry point of each page is stamped (``/static/js/app.js?v=…``), but the
modules it imports were asked for bare (``./grid.js``) and so had to be asked
about again on every visit: ``no-cache``, a 304 each. Modules import modules,
so those questions came one layer at a time — app.js, then grid.js, then
grid-base.js, then … — five or six round trips before the page could ask the
server anything. On the home network that is nothing. Through Tailscale from
somewhere else, at a fifth of a second a round trip, it was over a second of
every visit spent on files that had not changed.

Two things in the page put that right:

* an **import map** naming every module at its stamped URL, so ``./grid.js``
  is fetched as ``/static/js/grid.js?v=…`` — kept for a year, never asked
  about again until it changes, and then at a different URL so a new app.js
  can never run against last week's modules;
* a **modulepreload** link for every module the entry point needs at start,
  so the first visit after an update asks for all of them at once instead of
  layer by layer.

The import map is an inline script, which the page's Content-Security-Policy
forbids unless named; its hash goes into the policy for the response that
carries it (see ``csp_hashes``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from flask import g
from markupsafe import Markup

#: ``import … from './x.js'``, ``export … from './x.js'`` and ``import './x.js'``
#: — the static imports, the ones a module cannot run without.
_STATIC_IMPORT = re.compile(
    r"""(?:^|[;\s])(?:import|export)\s*(?:[\w*{}\s,$]+?\s*from\s*)?['"](\.{1,2}/[^'"]+)['"]""",
    re.M)
#: ``import('./x.js')`` — loaded when a feature is first opened.
_DYNAMIC_IMPORT = re.compile(r"""\bimport\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")


_GRAPHS: dict[tuple, list[Path]] = {}


def static_graph(entry: Path, js_root: Path, *, dynamic: bool = False) -> list[Path]:
    """Every module *entry* needs before it runs, entry first; with *dynamic*,
    also the ones it may load later, which the page can then fetch stamped.

    Worked out again only when a file under *js_root* changes, so a page
    view costs a look at the folder, not a read of every module."""
    try:
        state = tuple(sorted((str(p), p.stat().st_mtime_ns) for p in js_root.rglob("*")
                             if p.suffix in (".js", ".mjs")))
    except OSError:
        state = None
    key = (str(entry), str(js_root), dynamic, state)
    if state is not None and key in _GRAPHS:
        return list(_GRAPHS[key])
    graph = _walk(entry, js_root, dynamic)
    if state is not None:
        if len(_GRAPHS) > 32:
            _GRAPHS.clear()
        _GRAPHS[key] = graph
    return list(graph)


def _walk(entry: Path, js_root: Path, dynamic: bool) -> list[Path]:
    seen: dict[Path, None] = {}
    todo = [entry.resolve()]
    root = js_root.resolve()
    while todo:
        path = todo.pop(0)
        if path in seen or not path.is_file():
            continue
        seen[path] = None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        specs = _STATIC_IMPORT.findall(text)
        if dynamic:
            specs += _DYNAMIC_IMPORT.findall(text)
        for spec in specs:
            target = (path.parent / spec).resolve()
            if target.is_relative_to(root) and target not in seen:
                todo.append(target)
    return list(seen)


def page_modules(static_folder: Path, entry_url: str, asset: Callable[[str], str]) -> Markup:
    """The import map and preload links for the page whose entry is *entry_url*.

    *asset* is the template's own stamping function, so a module's ``?v=`` is
    the one the server answers with a year's caching (see ``_headers``).
    """
    js_root = static_folder / "js"
    def url(path: Path) -> str:
        return "/static/" + path.resolve().relative_to(static_folder.resolve()).as_posix()

    entry = static_folder / entry_url.removeprefix("/static/")
    # Only what this page can import: a plain script that happens to live in
    # js/ (leaflet.js, loaded when a map is opened) is not named here.
    reachable = static_graph(entry, js_root, dynamic=True)
    imports = {url(p): asset(url(p)) for p in sorted(reachable)}
    text = json.dumps({"imports": imports}, separators=(",", ":"), sort_keys=True)
    digest = base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")
    hashes = g.setdefault("importmap_hashes", [])
    if digest not in hashes:
        hashes.append(digest)

    preload = [asset(url(p)) for p in static_graph(entry, js_root)[1:]]
    lines = [f'<script type="importmap">{text}</script>']
    lines += [f'<link rel="modulepreload" href="{href}">' for href in preload]
    return Markup("\n".join(lines))


def csp_hashes() -> str:
    """The script-src additions for the import maps this response carries."""
    hashes = g.get("importmap_hashes") or []
    return "".join(f" 'sha256-{h}'" for h in hashes)
