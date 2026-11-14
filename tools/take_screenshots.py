#!/usr/bin/env python3
"""Retake every picture in docs/screens from the app as it is now.

    python tools/take_screenshots.py                 # all of them, into docs/screens
    python tools/take_screenshots.py --only console-tuning family-viewer
    python tools/take_screenshots.py --out /tmp/shots --keep

Builds the sample household the guides show (tools/make_sample.py's generated
scenes, "The Rivera Home", an administrator and two family members), starts a
Ninaivu of its own on it in a temporary folder with no AI, walks the first day
through the console the way a new household would, and photographs each page at
1200 x 750 in the light theme. Nobody's own photographs or settings are read:
the state folder is new and removed afterwards (``--keep`` leaves it, and the
server running, for a look around).

Needs ``playwright`` with its Chromium (``NINAIVU_CHROMIUM`` names another
browser) and ffmpeg for the two sample videos.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCREENS = ROOT / "docs" / "screens"
SIZE = {"width": 1200, "height": 750}

ADMIN = ("dad", "correcthorse1")
HOUSEHOLD = "The Rivera Home"
# Name, role, PIN: added on the first day's Household step, as a household would.
FAMILY = [("Maria", "2468"), ("Leo", "1357")]

# Console pages: picture name -> the sidebar tab it shows.
CONSOLE = {
    "console-overview": "overview",
    "console-archive": "archive",
    "console-library": "library",
    "console-folders": "folders",
    "console-large-files": "large-files",
    "console-uploads": "uploads",
    "console-screening": "screening",
    "console-straighten": "straighten",
    "console-people": "people",
    "console-visibility": "visibility",
    "console-faces": "faces",
    "console-cloud": "cloud",
    "console-health": "health",
    "console-restore": "restore",
    "console-handover": "handover",
    "console-ai-models": "ai-models",
    "console-ai-server": "ai-server",
    "console-settings": "settings",
    "console-server": "server",
    "console-activity": "activity",
    "console-performance": "performance",
    "console-tuning": "tuning",
    "console-advanced": "advanced",
    "console-migration": "migration",
}
FIRST_DAY = {"console-first-day": "computers", "console-first-day-ai": "ai"}
FAMILY_SHOTS = ["family-picker", "family-pin", "family-gallery", "family-viewer", "family-sudar"]
EVERY = ["console-sign-in", *FIRST_DAY, *CONSOLE, *FAMILY_SHOTS]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:     # noqa: S310 - our own local server
        return json.loads(r.read())


def wait_until(test, timeout: float, what: str) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if test():
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"gave up waiting for {what}")


def make_household(work: Path) -> tuple[Path, Path]:
    library = work / "Family Photos"
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_sample.py"), str(library),
                    "--count", "40"], check=True, stdout=subprocess.DEVNULL)
    state = work / "state"
    state.mkdir()
    # The household's name as the Home & extensions page would have saved it. The
    # first day is left to open, since two of the pictures are of it.
    # Creative Studio on, as the guides show it (Sudar's studio, the AI server page),
    # when it is installed here: pip install --no-deps -e extensions/creative-studio
    config = {"house_name": HOUSEHOLD}
    from ninaivu import extensions
    if "creative-studio" in extensions.discover():
        config["extensions"] = ["creative-studio"]
    (state / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return library, state


def wait_for_scan(page) -> None:
    """The first scan starts once the first day is through; the pages want it done.

    Asked from the signed-in console, since a visitor's counts leave out what
    is not public."""
    def scanned() -> bool:
        status = page.evaluate("fetch('/api/status').then(r => r.json())")
        stats, scan = status.get("stats", {}), status.get("scan", {})
        return stats.get("count", 0) >= 40 and scan.get("status") == "done"
    wait_until(scanned, 300, "the first scan")
    time.sleep(5)      # the passes after the count: thumbnails, duplicates, trips


def start_server(library: Path, state: Path, log: Path) -> tuple[subprocess.Popen, str, str]:
    home, admin = free_port(), free_port()
    env = dict(os.environ, NINAIVU_STATE_DIR=str(state), PYTHONUNBUFFERED="1",
               # The sample's drawn pictures are smaller than the floor that keeps
               # icons out of a real library.
               NINAIVU_MIN_MEDIA_BYTES="0", NINAIVU_STRAIGHTEN_AUTO="0")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ninaivu", str(library), "--ai", "off", "--no-watch",
         "--admin", ":".join(ADMIN), "--port", str(home), "--admin-port", str(admin)],
        cwd=str(ROOT), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT)
    home_url, admin_url = f"http://127.0.0.1:{home}", f"http://127.0.0.1:{admin}"
    wait_until(lambda: get_json(f"{home_url}/api/status"), 60, "Ninaivu to start")
    return proc, home_url, admin_url


class Camera:
    """Saves a page as the JPEG the guides use, if that picture was asked for."""

    def __init__(self, out: Path, wanted: set[str], paths: dict[str, str]):
        self.out, self.wanted, self.taken = out, wanted, []
        # Folders as a household would see them, not this run's temporary ones.
        self.paths = sorted(paths.items(), key=lambda kv: -len(kv[0]))

    def __call__(self, page, name: str) -> None:
        if name not in self.wanted:
            return
        from PIL import Image
        page.wait_for_timeout(400)
        page.evaluate("""pairs => {
            const swap = t => pairs.reduce((s, [a, b]) => s.split(a).join(b), t);
            const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            for (let n; (n = walk.nextNode());) {
                const t = swap(n.nodeValue);
                if (t !== n.nodeValue) n.nodeValue = t;
            }
            document.querySelectorAll('input, textarea').forEach(i => {
                i.value = swap(i.value);
                if (i.placeholder) i.placeholder = swap(i.placeholder);
            });
        }""", self.paths)
        png = page.screenshot(animations="disabled", caret="initial")
        Image.open(io.BytesIO(png)).convert("RGB").save(
            self.out / f"{name}.jpg", "JPEG", quality=86, optimize=True, progressive=True)
        self.taken.append(name)
        print(f"  {name}.jpg")


def light_context(browser):
    ctx = browser.new_context(viewport=SIZE, color_scheme="light", locale="en-GB")
    # The theme and language are kept in the browser, JSON-encoded.
    ctx.add_init_script("try { localStorage.setItem('ninaivu.theme', '\"light\"');"
                        " localStorage.setItem('ninaivu.lang', '\"en\"'); } catch (e) {}")
    return ctx


def console(browser, home_url: str, admin_url: str, shoot: Camera) -> None:
    ctx = light_context(browser)
    page = ctx.new_page()
    page.goto(admin_url, wait_until="networkidle")
    page.wait_for_selector("#gate input[type='password']")
    shoot(page, "console-sign-in")
    page.fill("#gate input[type='text']", ADMIN[0])
    page.fill("#gate input[type='password']", ADMIN[1])
    page.click("#gate .btn.primary")

    # The first day, step by step, as a new household walks it.
    page.wait_for_selector("#first-day:not([hidden])", timeout=20000)
    page.wait_for_timeout(1200)
    for _ in range(12):
        step = page.evaluate("(document.querySelector('#fd-steps li.current') || {}).dataset?.step")
        for name, wanted_step in FIRST_DAY.items():
            if step == wanted_step:
                shoot(page, name)
        if step == "people":
            for who, pin in FAMILY:
                body = page.locator("#fd-body")
                body.locator("input").nth(0).fill(who)
                body.locator("input[inputmode='numeric']").first.fill(pin)
                body.get_by_role("button", name="Add").click()
                page.wait_for_timeout(700)
        nxt = page.locator("#fd-next")
        if not nxt.is_visible():
            break
        nxt.click()
        page.wait_for_timeout(900)
        if page.locator("#first-day").is_hidden():
            break
    wait_for_scan(page)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2000)
    # The toast the first day leaves behind would sit on the first page.
    page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")

    for name, tab in CONSOLE.items():
        if name not in shoot.wanted:
            continue
        button = page.locator(f"#tabs button[data-tab='{tab}']")
        if not button.is_visible():
            print(f"  {name}: the page is not on this console (an extension it needs is off)")
            continue
        button.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        page.evaluate("""tab => {
            document.querySelectorAll('.toast').forEach(t => t.remove());
            const b = document.querySelector(`#tabs button[data-tab='${tab}']`);
            if (b) b.scrollIntoView({block: 'center'});
            window.scrollTo(0, 0);
            document.querySelectorAll('main, .main, .content').forEach(m => m.scrollTop = 0);
        }""", tab)
        shoot(page, name)
    ctx.close()


def family(browser, home_url: str, shoot: Camera) -> None:
    ctx = light_context(browser)
    page = ctx.new_page()
    page.goto(home_url, wait_until="networkidle")
    page.wait_for_timeout(1500)
    shoot(page, "family-picker")
    who, pin = FAMILY[0]
    page.get_by_text(who, exact=True).first.click()
    page.wait_for_timeout(800)
    page.keyboard.type(pin[:2])
    page.wait_for_timeout(300)
    shoot(page, "family-pin")
    page.keyboard.type(pin[2:])
    page.keyboard.press("Enter")
    page.wait_for_timeout(2500)

    # An album the family made, so the sidebar has one.
    page.evaluate("""async () => {
        const r = await fetch('/api/assets?limit=100&sort=date_desc');
        const j = await r.json();
        const items = (j.items || j.assets || j).filter(a => (a.kind || a.type || 'photo') !== 'video');
        const ids = items.slice(0, 16).map(a => a.id);
        await fetch('/api/albums', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                   body: JSON.stringify({name: 'Summer 2025', ids})});
    }""")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(2500)
    shoot(page, "family-gallery")

    page.locator(".tile img, .grid img").nth(5).click()
    page.wait_for_timeout(2500)
    shoot(page, "family-viewer")

    sudar = page.get_by_role("button", name="Sudar")
    if not sudar.count():
        sudar = page.locator("[data-action='edit'], button[title*='Sudar'], button:has-text('Edit')")
    sudar.first.click()
    page.wait_for_timeout(4000)
    shoot(page, "family-sudar")
    ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=SCREENS)
    ap.add_argument("--only", nargs="*", default=None, help="picture names, without .jpg")
    ap.add_argument("--keep", action="store_true", help="leave the server and its folder")
    args = ap.parse_args()
    wanted = set(args.only or EVERY)
    unknown = wanted - set(EVERY)
    if unknown:
        ap.error(f"no such picture: {', '.join(sorted(unknown))}")
    args.out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="ninaivu-screens-")).resolve()
    library, state = make_household(work)
    proc, home_url, admin_url = start_server(library, state, work / "server.log")
    print(f"Ninaivu on the sample household: {home_url}  console {admin_url}")
    shoot = Camera(args.out, wanted, {str(work): "~", str(Path.home()): "~"})
    try:
        with sync_playwright() as p:
            exe = os.environ.get("NINAIVU_CHROMIUM")
            browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
            console(browser, home_url, admin_url, shoot)
            family(browser, home_url, shoot)
            browser.close()
    finally:
        if args.keep:
            print(f"left running (pid {proc.pid}); folder {work}")
        else:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(work, ignore_errors=True)
    missing = sorted(wanted - set(shoot.taken))
    if missing:
        print(f"not taken: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
