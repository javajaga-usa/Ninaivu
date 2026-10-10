"""The family archive: the library on a USB drive, readable with nothing but a browser.

Every other copy Ninaivu keeps needs Ninaivu, or somebody who knows what
Ninaivu was, to make sense of it. The second copy (``mirror.py``) is plain
files, but plain files are 200,000 names in date folders, with nothing to say
who is in them. This one is a folder anybody can open:

* ``Open me.html``: the photographs by month, with the people in them, the
  albums, the places, and a search box; click one to see it full size, arrow
  keys to go on. Plain HTML and script, no server, no internet: it works from
  the drive in any browser today and, being a web page, for a long time to
  come.
* ``Read me first.html``: what this is, when it was made, and a letter from
  the household to whoever opens it (who to ask, where the rest is kept, what
  matters). Written on the console's Handover page. Never a password or a key.
* ``photos/``: the photographs and videos themselves, in their own folders,
  under their own names (or smaller copies of the photographs, for a small
  drive).
* ``thumbs/``: little JPEGs for the page. ``data.js``: the list the page
  reads: dates, people, albums, places.

Which photographs go in is chosen when it is made: those the family sees (the
default), or everything including what is hidden. Flagged items, sound
recordings and the recycle bin never go. Made again onto the same drive, it
copies only what is new or changed, and nothing already on the drive is
removed.

Ported from Hearth's ``storage/keepsake.py`` (30 Sep 2026), with these
changes: the drive is checked by the second copy's own rule
(:func:`mirror.folder_problem`); a stopped run leaves the page from the last
whole run in place instead of a page listing only what this run reached;
smaller copies of ``a.png`` and ``a.jpg`` no longer land on one file; a
changed photograph is copied again in small mode too; videos are counted
against the free space in small mode; and the page opens files whose names
carry ``#`` or ``?``.
"""

from __future__ import annotations

import html
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .mirror import folder_problem

log = logging.getLogger(__name__)

__all__ = ["Keepsake", "FOLDER"]

FOLDER = "Family photo archive"
#: Room kept free on the drive, so the archive never fills it to the last byte.
RESERVE = 200 * 1024 * 1024
#: Longest side of a smaller copy, when originals will not fit.
SMALL = 2048
#: The longest letter, in characters. A letter, not a book.
LETTER_MAX = 20000


class Keepsake:
    def __init__(self, cfg, connect_db: Callable[[], sqlite3.Connection], *,
                 hold: Callable[[], Any] | None = None):
        self.cfg = cfg
        self._connect = connect_db
        self._hold = hold
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False, "phase": "", "done": 0, "total": 0,
                                       "copied": 0, "bytes": 0, "message": "", "error": "",
                                       "where": "", "problems": []}

    @property
    def roots(self) -> list[str]:
        return list(self.cfg.roots or ([self.cfg.active_root] if self.cfg.active_root else []))

    def status(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._state, problems=list(self._state["problems"]))
        snap["letter"] = str(getattr(self.cfg, "keepsake_letter", "") or "")
        snap["last_folder"] = str(getattr(self.cfg, "keepsake_folder", "") or "")
        snap["last_made"] = float(getattr(self.cfg, "keepsake_made", 0) or 0)
        return snap

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def problem(self, folder: str) -> str | None:
        """Why the archive cannot be made in *folder*, or None if it can."""
        if not str(folder or "").strip():
            return "Choose the drive or folder to make it in."
        found = folder_problem(folder, self.roots, self.cfg.state_dir)
        if found and "second copy" in found:
            # The second copy's wording, said of this instead.
            return "That folder is inside the library or Ninaivu's own folder. Make it on another drive."
        return found

    def plan(self, conn: sqlite3.Connection, everything: bool) -> list[dict[str, Any]]:
        """What goes in, oldest first."""
        marks = ",".join("?" * len(self.roots)) or "''"
        rows = conn.execute(
            "SELECT id, root, rel_path, filename, kind, size, mtime, date_key, width, height, "
            "city, country, thumb, visibility FROM assets "
            "WHERE trashed = 0 AND nsfw = 0 AND live_clip = 0 AND kind IN ('picture', 'video') "
            f"AND root IN ({marks}) {'' if everything else 'AND visibility <= 1'} "
            "ORDER BY COALESCE(captured_at, mtime), id", self.roots).fetchall()
        return [dict(r) for r in rows]

    def save_letter(self, letter: str) -> None:
        self.cfg.keepsake_letter = str(letter)[:LETTER_MAX]
        self._save()

    def _save(self) -> None:
        try:
            self.cfg.save()
        except Exception:                                   # noqa: BLE001 — a test config
            log.debug("the family archive's settings were not saved", exc_info=True)

    def start(self, folder: str, *, everything: bool = False, small: bool = False,
              letter: str | None = None) -> dict[str, Any]:
        problem = self.problem(folder)
        if problem:
            raise ValueError(problem)
        with self._lock:
            if self.running:
                raise ValueError("The archive is already being made.")
            if letter is not None:
                self.cfg.keepsake_letter = str(letter)[:LETTER_MAX]
            self.cfg.keepsake_folder = str(folder)
            self._stop.clear()
            self._state.update(running=True, phase="Choosing the photographs…", done=0, total=0,
                               copied=0, bytes=0, message="", error="", problems=[],
                               where=str(Path(folder).expanduser() / FOLDER))
            self._thread = threading.Thread(target=self._run, args=(folder, everything, small),
                                            name="ninaivu-keepsake", daemon=True)
            self._thread.start()
        self._save()
        return self.status()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(15)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _problem(self, name: str, why: str) -> None:
        with self._lock:
            if len(self._state["problems"]) < 50:
                self._state["problems"].append({"file": name, "why": why})

    def _pause(self, phase: str) -> None:
        """Wait while people are using Ninaivu (the workload's say), or until stopped."""
        waited = False
        while self._hold is not None and not self._stop.is_set():
            reason = self._hold()
            if not reason:
                break
            waited = True
            self._update(phase=f"Waiting: {reason}")
            self._stop.wait(5)
        if waited:
            self._update(phase=phase)

    # -- the work ----------------------------------------------------------------

    def _run(self, folder: str, everything: bool, small: bool) -> None:
        message = ""
        conn = None
        try:
            conn = self._connect()
            items = self.plan(conn, everything)
            several = len({i["root"] for i in items}) > 1
            base = Path(folder).expanduser() / FOLDER
            (base / "photos").mkdir(parents=True, exist_ok=True)
            (base / "thumbs").mkdir(parents=True, exist_ok=True)
            needed = sum(int(i["size"] or 0) for i in items
                         if (not small or i["kind"] == "video")
                         and not self._already(base, i, small, several))
            free = shutil.disk_usage(base).free
            if needed + RESERVE > free:
                advice = "a bigger drive" if small else "smaller copies, or a bigger drive"
                raise OSError(f"what is left to copy needs {needed / 1e9:.1f} GB and the drive has "
                              f"{free / 1e9:.1f} GB free; choose {advice}")
            phase = "Copying the photographs…"
            self._update(total=len(items), phase=phase)
            names = self._names(conn, [i["id"] for i in items])
            entries = []
            stopped = False
            for index, item in enumerate(items):
                if self._stop.is_set():
                    stopped = True
                    break
                if index % 50 == 0:
                    self._pause(phase)
                entry = self._one(base, item, small, several)
                if entry is not None:
                    entry.update(names.get(item["id"], {}))
                    entries.append(entry)
                self._update(done=index + 1)
            if stopped:
                # A page listing only what this run reached would hide the
                # rest of an archive made whole before: leave that one.
                if not (base / "data.js").is_file():
                    self._pages(base, entries)
                message = "Stopped. Making it again carries on where it stopped."
            else:
                self._update(phase="Writing the pages…")
                self._pages(base, entries)
                self.cfg.keepsake_made = time.time()
                self._save()
                snap = self.status()
                message = (f"The archive holds {len(entries):,} photographs and videos "
                           f"({snap['copied']:,} copied this time). Open “Open me.html” on the drive.")
        except Exception as exc:                            # noqa: BLE001
            log.exception("making the family archive stopped")
            self._update(error=f"It stopped: {exc}")
        finally:
            if conn is not None:
                conn.close()
            self._update(running=False, phase="", message=message)

    @staticmethod
    def _target(base: Path, item: dict[str, Any], small: bool, several: bool) -> Path:
        parts = ([Path(item["root"]).name or "library"] if several else []) + \
            str(item["rel_path"]).replace("\\", "/").split("/")
        target = base.joinpath("photos", *parts)
        if small and item["kind"] == "picture" and target.suffix.lower() not in (".jpg", ".jpeg"):
            # IMG_1.png becomes IMG_1.png.jpg, so it never lands on IMG_1.jpg
            # beside it.
            target = target.with_name(target.name + ".jpg")
        return target

    @staticmethod
    def _current(target: Path, item: dict[str, Any], small: bool) -> bool:
        """Whether *target* already holds this version of the item."""
        try:
            stat = target.stat()
        except OSError:
            return False
        if abs(stat.st_mtime - float(item["mtime"] or 0)) > 1:
            return False
        return (small and item["kind"] == "picture") or stat.st_size == int(item["size"] or 0)

    def _already(self, base: Path, item: dict[str, Any], small: bool, several: bool) -> bool:
        return self._current(self._target(base, item, small, several), item, small)

    def _one(self, base: Path, item: dict[str, Any], small: bool, several: bool) -> dict[str, Any] | None:
        source = Path(item["root"]).joinpath(*str(item["rel_path"]).replace("\\", "/").split("/"))
        target = self._target(base, item, small, several)
        if not Path(os.path.abspath(target)).is_relative_to(os.path.abspath(base / "photos")):
            return None
        thumb = base / "thumbs" / f"{item['id']}.jpg"
        try:
            if not self._current(target, item, small):
                if not source.is_file():
                    raise OSError("the file is not there")
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(f".{target.name}.part")
                try:
                    if small and item["kind"] == "picture":
                        self._smaller(source, partial)
                    else:
                        shutil.copyfile(source, partial)
                    stamp = float(item["mtime"] or 0)
                    os.utime(partial, (stamp, stamp))
                    os.replace(partial, target)
                finally:
                    partial.unlink(missing_ok=True)
                with self._lock:
                    self._state["copied"] += 1
                    self._state["bytes"] += target.stat().st_size
                thumb.unlink(missing_ok=True)
            if not thumb.is_file():
                self._thumb(item, source, thumb)
        except (OSError, ValueError) as exc:
            self._problem(item["rel_path"], str(exc))
            return None
        return {
            "id": item["id"], "f": target.relative_to(base).as_posix(),
            "t": f"thumbs/{item['id']}.jpg" if thumb.is_file() else "",
            "d": item["date_key"] or "", "k": "v" if item["kind"] == "video" else "p",
            "w": item["width"] or 0, "h": item["height"] or 0,
            "place": ", ".join(x for x in (item["city"], item["country"]) if x),
            "n": item["filename"],
        }

    @staticmethod
    def _smaller(source: Path, out: Path) -> None:
        from ..media import media                           # noqa: PLC0415
        with media._open_oriented(source) as image:          # noqa: SLF001
            image = image.convert("RGB")
            image.thumbnail((SMALL, SMALL))
            image.save(out, "JPEG", quality=88)

    def _thumb(self, item: dict[str, Any], source: Path, out: Path) -> None:
        """The index's own thumbnail as a JPEG (the most readable format there is)."""
        from PIL import Image                               # noqa: PLC0415

        from ..media import media                           # noqa: PLC0415
        try:
            if item.get("thumb"):
                size = max(self.cfg.thumb_sizes)
                stored = self.cfg.thumbs_dir / media.thumb_file(item["thumb"], size, self.cfg.thumb_format)
                if stored.is_file():
                    with Image.open(stored) as image:
                        image.convert("RGB").save(out, "JPEG", quality=82)
                    return
            if item["kind"] == "picture":
                with media._open_oriented(source) as image:  # noqa: SLF001
                    image = image.convert("RGB")
                    image.thumbnail((640, 640))
                    image.save(out, "JPEG", quality=82)
        except Exception:                                   # noqa: BLE001 — no thumbnail is not a failure
            log.debug("no thumbnail for %s in the family archive", item["rel_path"], exc_info=True)
            out.unlink(missing_ok=True)

    @staticmethod
    def _names(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Who is in each photograph and which albums it is in, by name."""
        found: dict[int, dict[str, Any]] = defaultdict(lambda: {"people": [], "albums": []})
        wanted = set(ids)
        for r in conn.execute(
                "SELECT DISTINCT f.asset_id, p.name FROM faces f JOIN people_clusters p ON p.id = f.person_id "
                "WHERE f.source IN ('confirmed', 'auto') AND COALESCE(p.name, '') != ''"):
            if r["asset_id"] in wanted:
                found[r["asset_id"]]["people"].append(r["name"])
        for r in conn.execute("SELECT ai.asset_id, al.name FROM album_items ai JOIN albums al "
                              "ON al.id = ai.album_id"):
            if r["asset_id"] in wanted:
                found[r["asset_id"]]["albums"].append(r["name"])
        return found

    def _pages(self, base: Path, entries: list[dict[str, Any]]) -> None:
        from ..server.config import house_name              # noqa: PLC0415
        people = sorted({p for e in entries for p in e.get("people", [])})
        albums = sorted({a for e in entries for a in e.get("albums", [])})
        index_people = {name: i for i, name in enumerate(people)}
        index_albums = {name: i for i, name in enumerate(albums)}
        for e in entries:
            e["p"] = [index_people[n] for n in e.pop("people", [])]
            e["a"] = [index_albums[n] for n in e.pop("albums", [])]
        title = house_name(self.cfg)
        data = {"title": title, "made": time.strftime("%Y-%m-%d"), "people": people, "albums": albums,
                "items": entries}
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        _write(base / "data.js", f"window.NINAIVU_ARCHIVE = {payload};\n")
        _write(base / "Open me.html", _VIEWER.replace("{{TITLE}}", html.escape(title)))
        letter = str(getattr(self.cfg, "keepsake_letter", "") or "").strip()
        letter_html = "".join(f"<p>{html.escape(p)}</p>" for p in letter.split("\n\n") if p.strip()) \
            or "<p><em>No letter was written.</em></p>"
        _write(base / "Read me first.html", _README.format(
            title=html.escape(title), made=time.strftime("%d %B %Y"), count=f"{len(entries):,}",
            letter=letter_html))


def _write(path: Path, text: str) -> None:
    """Whole or not at all: a drive pulled out mid-write keeps the page it had."""
    partial = path.with_name(f".{path.name}.part")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


_README = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Read me first: {title}</title>
<style>
:root {{ color-scheme: light dark; --bg: #fbf8f2; --text: #222; --muted: #6f6a60; --accent: #9a6a1f;
  --line: #c9a56a; --code: #efe8da; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #17150f; --text: #ece6da; --muted: #a49c8c;
  --accent: #e0b56a; --line: #7a6136; --code: #2a261d; }} }}
body {{ font: 17px/1.6 Georgia, 'Times New Roman', serif; max-width: 680px; margin: 40px auto; padding: 0 20px;
  color: var(--text); background: var(--bg); }}
h1 {{ font-size: 28px; margin-bottom: 4px; }} .made {{ color: var(--muted); margin-top: 0; }}
.letter {{ border-left: 3px solid var(--line); padding-left: 18px; margin: 24px 0; white-space: pre-line; }}
a {{ color: var(--accent); }} code {{ background: var(--code); padding: 1px 5px; border-radius: 4px; }}
</style></head><body>
<h1>{title}: the family photographs</h1>
<p class="made">Made on {made}, holding {count} photographs and videos.</p>
<p>This drive is a copy of the family's photographs, made so that they can be looked at without any
special program: open <a href="Open me.html"><code>Open me.html</code></a> in a web browser (double-click
it) and they are there, by month, with the people in them and the places they were taken.</p>
<p>The photographs themselves are in the <code>photos</code> folder, as ordinary files, in their own
folders and under their own names. They can be copied anywhere.</p>
<h2>A letter from the family</h2>
<div class="letter">{letter}</div>
<p style="color:var(--muted);font-size:14px">Made by Ninaivu, the program the family kept its photographs in.
Nothing on this drive needs Ninaivu to be read.</p>
</body></html>
"""

_VIEWER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}: family photographs</title>
<style>
:root { color-scheme: light dark; --bg: #f6f4f0; --text: #1d1d1f; --muted: #6b6b70; --card: #fff; --line: #e3dfd6; }
@media (prefers-color-scheme: dark) { :root { --bg: #151517; --text: #eee; --muted: #9a9aa0; --card: #1f1f22; --line: #2d2d31; } }
* { box-sizing: border-box; } body { margin: 0; font: 15px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text); }
header { position: sticky; top: 0; z-index: 2; background: var(--bg); border-bottom: 1px solid var(--line);
  padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
header h1 { font-size: 18px; margin: 0 12px 0 0; }
header a { color: var(--muted); font-size: 14px; }
select, input { font: inherit; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--card); color: var(--text); max-width: 100%; }
main { padding: 12px 16px 40px; } h2 { font-size: 16px; margin: 22px 0 8px; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 6px; }
.grid button { position: relative; padding: 0; border: 0; background: var(--card); aspect-ratio: 1;
  border-radius: 6px; overflow: hidden; cursor: pointer; }
.grid img { width: 100%; height: 100%; object-fit: cover; display: block; }
.grid .v::after { content: "\\25B6"; position: absolute; right: 6px; bottom: 4px; color: #fff; text-shadow: 0 1px 4px #000; }
.grid .noimg { display: grid; place-items: center; height: 100%; color: var(--muted); font-size: 12px; padding: 6px;
  overflow-wrap: anywhere; }
#count { color: var(--muted); }
.more { display: block; margin: 24px auto; font: inherit; padding: 8px 16px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--card); color: var(--text); cursor: pointer; }
.shade { position: fixed; inset: 0; background: rgba(0,0,0,.92); display: grid; grid-template-rows: 1fr auto;
  z-index: 5; } .shade[hidden] { display: none; }
.stage { display: grid; place-items: center; min-height: 0; padding: 12px; }
.stage img, .stage video { max-width: 100%; max-height: calc(100vh - 110px); }
.about { color: #ddd; padding: 10px 16px 16px; text-align: center; font-size: 14px; overflow-wrap: anywhere; }
.nav { position: fixed; top: 50%; transform: translateY(-50%); font-size: 40px; color: #fff; background: none;
  border: 0; cursor: pointer; padding: 20px; } #prev { left: 0; } #next { right: 0; }
#close { position: fixed; top: 8px; right: 12px; font-size: 30px; color: #fff; background: none; border: 0; cursor: pointer; }
</style></head><body>
<header>
  <h1>{{TITLE}}</h1>
  <select id="year" aria-label="Year"><option value="">Every year</option></select>
  <select id="person" aria-label="Person"><option value="">Everybody</option></select>
  <select id="album" aria-label="Album"><option value="">Every album</option></select>
  <input id="q" type="search" placeholder="Search places and names" aria-label="Search">
  <span id="count"></span>
  <a href="Read me first.html">Read me first</a>
</header>
<main id="main"></main>
<div class="shade" id="shade" hidden>
  <div class="stage" id="stage"></div>
  <div class="about" id="about"></div>
  <button class="nav" id="prev" aria-label="Previous">&#8249;</button>
  <button class="nav" id="next" aria-label="Next">&#8250;</button>
  <button id="close" aria-label="Close">&#215;</button>
</div>
<script src="data.js"></script>
<script>
(function () {
  var data = window.NINAIVU_ARCHIVE || { items: [], people: [], albums: [] };
  var $ = function (id) { return document.getElementById(id); };
  var PAGE = 600;
  var shown = [], at = -1, drawn = 0, group = null, grid = null;
  function option(select, value, text) { var o = document.createElement('option'); o.value = value; o.textContent = text; select.appendChild(o); }
  // A file name is a name, not an address: "#" and "?" in it are letters.
  function href(path) { return path.split('/').map(encodeURIComponent).join('/'); }
  var years = {};
  data.items.forEach(function (i) { if (i.d) years[i.d.slice(0, 4)] = 1; });
  Object.keys(years).sort().reverse().forEach(function (y) { option($('year'), y, y); });
  data.people.forEach(function (p, n) { option($('person'), String(n), p); });
  data.albums.forEach(function (a, n) { option($('album'), String(n), a); });
  if (!data.albums.length) $('album').hidden = true;
  if (!data.people.length) $('person').hidden = true;
  function words(i) {
    return (i.place + ' ' + i.n + ' ' + i.p.map(function (n) { return data.people[n]; }).join(' ') + ' '
      + i.a.map(function (n) { return data.albums[n]; }).join(' ')).toLowerCase();
  }
  function month(key) {
    if (key.length !== 7) return key;
    var when = new Date(key + '-01T12:00:00');
    return isNaN(when) ? key : when.toLocaleDateString(undefined, { year: 'numeric', month: 'long' });
  }
  function more() {
    var main = $('main'), old = $('more');
    if (old) old.remove();
    var end = Math.min(drawn + PAGE, shown.length);
    for (var n = drawn; n < end; n++) {
      var item = shown[n];
      var key = (item.d || 'No date').slice(0, 7);
      if (key !== group) {
        group = key;
        var h = document.createElement('h2');
        h.textContent = month(key);
        grid = document.createElement('div'); grid.className = 'grid';
        main.appendChild(h); main.appendChild(grid);
      }
      var b = document.createElement('button');
      b.setAttribute('aria-label', item.n);
      if (item.k === 'v') b.className = 'v';
      if (item.t) { var img = document.createElement('img'); img.loading = 'lazy'; img.src = href(item.t); img.alt = ''; b.appendChild(img); }
      else { var s = document.createElement('span'); s.className = 'noimg'; s.textContent = item.n; b.appendChild(s); }
      b.onclick = (function (index) { return function () { open(index); }; })(n);
      grid.appendChild(b);
    }
    drawn = end;
    if (drawn < shown.length) {
      var button = document.createElement('button');
      button.id = 'more'; button.className = 'more';
      button.textContent = 'Show more (' + (shown.length - drawn) + ' left)';
      button.onclick = more;
      main.appendChild(button);
    }
  }
  function draw() {
    var y = $('year').value, p = $('person').value, a = $('album').value, q = $('q').value.trim().toLowerCase();
    shown = data.items.filter(function (i) {
      return (!y || (i.d || '').slice(0, 4) === y) && (!p || i.p.indexOf(Number(p)) >= 0)
        && (!a || i.a.indexOf(Number(a)) >= 0) && (!q || words(i).indexOf(q) >= 0);
    }).reverse();
    $('count').textContent = shown.length + ' of ' + data.items.length;
    $('main').innerHTML = '';
    drawn = 0; group = null; grid = null;
    more();
  }
  function open(n) {
    if (!shown.length) return;
    at = (n + shown.length) % shown.length;
    var item = shown[at], stage = $('stage');
    stage.innerHTML = '';
    var node = document.createElement(item.k === 'v' ? 'video' : 'img');
    node.src = href(item.f);
    if (item.k === 'v') { node.controls = true; node.autoplay = true; } else { node.alt = item.n; }
    stage.appendChild(node);
    var bits = [item.d, item.place, item.p.map(function (x) { return data.people[x]; }).join(', ')].filter(Boolean);
    $('about').textContent = bits.join(' \\u00b7 ') + '   (' + item.f + ')';
    $('shade').hidden = false;
  }
  function close() { $('shade').hidden = true; $('stage').innerHTML = ''; }
  $('prev').onclick = function () { open(at - 1); };
  $('next').onclick = function () { open(at + 1); };
  $('close').onclick = close;
  document.addEventListener('keydown', function (e) {
    if ($('shade').hidden) return;
    if (e.key === 'ArrowLeft') open(at - 1); else if (e.key === 'ArrowRight') open(at + 1); else if (e.key === 'Escape') close();
  });
  ['year', 'person', 'album'].forEach(function (id) { $(id).onchange = draw; });
  $('q').oninput = draw;
  draw();
})();
</script>
</body></html>
"""
