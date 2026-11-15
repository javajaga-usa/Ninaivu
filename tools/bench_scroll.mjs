/**
 * What a fast scroll shows: on every frame, what each part of the screen was
 * showing — the photograph, a blurred preview of it, or an empty tile (its
 * colour, or nothing at all).
 *
 * Run through tools/bench_scroll.py, which builds a library of 200,000 items
 * and serves it; or against any family app with NINAIVU_HOME, NINAIVU_USER and
 * NINAIVU_PASS set the way the browser tests read them.
 *
 *   node tools/bench_scroll.mjs [--profiles desktop,phone] [--runs 1]
 *                               [--rtt 300] [--down 2000] [--up 750] [--seconds 4]
 *
 * Two profiles:
 *   desktop  1440x900 at 2x, straight to the server (the home network)
 *   phone    390x844 at 3x, touch, CPU slowed 4x, over a slow link
 *            (tests/slow_link.mjs: --rtt ms round trip, --down/--up kbit/s,
 *            the family in India reaching the Mac mini over Tailscale)
 *
 * In each: a scroll at a reading pace, a flick and a fling, each into a stretch
 * of the library not seen before, and a jump far away (the date rail let go)
 * timed until the screen has no empty tile and until it has every photograph.
 *
 * Every tile on screen is looked at on every frame, weighted by how much of
 * the screen it covers, so the figures are shares of the photographs' area
 * over time; a tile not made yet counts as empty, as one still waiting does.
 */
import { launch, HOME, ADMIN_USER, ADMIN_PASS } from '../tests/harness.mjs';
import { slowLink } from '../tests/slow_link.mjs';

const args = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = args.indexOf(`--${name}`);
  return at >= 0 ? args[at + 1] : fallback;
};
const PROFILES = String(arg('profiles', 'desktop,phone')).split(',');
const RUNS = Number(arg('runs', 1));
const RTT = Number(arg('rtt', 300));
const DOWN = Number(arg('down', 2000)) * 1000 / 8;
const UP = Number(arg('up', 750)) * 1000 / 8;
const SECONDS = Number(arg('seconds', 4));

const PROFILE = {
  desktop: {
    context: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 },
    cpu: 1,
    slow: false,
    // px per ms: a reading pace, a flick, a fling.
    speeds: { reading: 1.5, flick: 5, fling: 12 },
  },
  phone: {
    context: {
      viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true,
      userAgent: 'Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 '
        + '(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36',
    },
    cpu: 4,
    slow: true,
    speeds: { reading: 1, flick: 3, fling: 8 },
  },
};

async function signIn(page, url) {
  await page.goto(url, { waitUntil: 'load', timeout: 120000 });
  const status = await page.evaluate(async ([username, password]) => {
    const answer = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    return answer.status;
  }, [ADMIN_USER, ADMIN_PASS]);
  if (status !== 200) throw new Error(`sign in: ${status}`);
  await page.goto(url, { waitUntil: 'load', timeout: 120000 });
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell img.ready').length > 4,
    null, { timeout: 120000, polling: 100 });
}

/** Before the app starts: keep hold of the grid, which the app's own
 *  scroll listener is the first thing to touch. */
function catchGrid() {
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function addEventListener(...rest) {
    if (!window.__grid && this && this.layout && this.mounted instanceof Map) window.__grid = this;
    return add.apply(this, rest);
  };
}

/** In the page: what the tiles on screen show, by area. */
function installSampler() {
  window.__sample = () => {
    const grid = window.__grid;
    const scroller = grid.scroller;
    const top = scroller.scrollTop;
    const bottom = top + scroller.clientHeight;
    const cells = grid.layout.cells;
    // The first tile reaching the screen (cells are in order down the page).
    let lo = 0;
    let hi = cells.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (cells[mid].y + cells[mid].h <= top) lo = mid + 1; else hi = mid;
    }
    const counts = { photo: 0, preview: 0, empty: 0 };
    for (let i = Math.max(0, lo - 50); i < cells.length; i += 1) {
      const cell = cells[i];
      if (cell.y > bottom) break;
      if (cell.y + cell.h <= top || (cell.flags & 2) === 0) continue;
      const area = cell.w * (Math.min(bottom, cell.y + cell.h) - Math.max(top, cell.y));
      const node = grid.mounted.get(cell.n);
      const img = node && node.firstElementChild;
      if (img && !img.hidden && img.classList.contains('ready') && img.complete && img.naturalWidth > 0) {
        counts.photo += area;
      } else if (node && /url\(/.test(node.style.backgroundImage || '')) {
        counts.preview += area;
      } else {
        counts.empty += area;
      }
    }
    return counts;
  };

  /** Scroll down at *speed* px/ms for *ms*, sampling every frame. */
  window.__scroll = (speed, ms) => new Promise((resolve) => {
    const scroller = document.querySelector('#scroller');
    const totals = { photo: 0, preview: 0, empty: 0 };
    const gaps = [];
    let emptyFrames = 0;
    let frames = 0;
    let start = 0;
    let last = 0;
    const step = (at) => {
      if (!start) { start = at; last = at; }
      const counts = window.__sample();
      for (const key of Object.keys(totals)) totals[key] += counts[key];
      if (counts.empty) emptyFrames += 1;
      frames += 1;
      gaps.push(at - last);
      scroller.scrollTop += speed * (at - last);
      last = at;
      if (at - start < ms) requestAnimationFrame(step);
      else resolve({ totals, frames, emptyFrames, gaps, end: scroller.scrollTop });
    };
    requestAnimationFrame(step);
  });

  /** Jump to *top*; how long until no empty point, and until all photographs. */
  window.__jump = (top, limit) => new Promise((resolve) => {
    const scroller = document.querySelector('#scroller');
    const started = performance.now();
    let covered = null;
    scroller.scrollTop = top;
    const step = (at) => {
      const counts = window.__sample();
      const took = at - started;
      if (covered == null && !counts.empty) covered = took;
      if (!counts.empty && !counts.preview) { resolve({ covered: covered ?? took, sharp: took }); return; }
      if (took > limit) { resolve({ covered, sharp: null }); return; }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

const pct = (n, total) => (total ? (100 * n / total) : 0);

async function runProfile(browser, name) {
  const profile = PROFILE[name];
  const link = profile.slow ? await slowLink(HOME, { rtt: RTT, down: DOWN, up: UP }) : null;
  const url = link ? link.url : HOME;
  const context = await browser.newContext(profile.context);
  const page = await context.newPage();
  await page.addInitScript(catchGrid);
  await signIn(page, url);
  const cdp = await context.newCDPSession(page);
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: profile.cpu });
  await page.evaluate(installSampler);
  const view = await page.evaluate(() => document.querySelector('#scroller').clientHeight);

  const results = {};
  let at = view * 4;
  for (const [pace, speed] of Object.entries(profile.speeds)) {
    // Somewhere not seen before; let it settle as a person would, then go.
    await page.evaluate(([top]) => { document.querySelector('#scroller').scrollTop = top; }, [at]);
    await page.waitForFunction(() => {
      const c = window.__sample();
      return !c.empty && !c.preview;
    }, null, { timeout: 30000, polling: 100 }).catch(() => {});
    await page.waitForTimeout(1200);
    const bytes = link ? link.down.bytes : 0;
    const r = await page.evaluate(([s, ms]) => window.__scroll(s, ms), [speed, SECONDS * 1000]);
    const sum = r.totals.photo + r.totals.preview + r.totals.empty;
    const sorted = r.gaps.slice(1).sort((a, b) => a - b);
    results[pace] = {
      speed,
      photo: pct(r.totals.photo, sum),
      preview: pct(r.totals.preview, sum),
      empty: pct(r.totals.empty, sum),
      emptyFrames: pct(r.emptyFrames, r.frames),
      p95frame: sorted[Math.floor(sorted.length * 0.95)] || 0,
      kb: link ? (link.down.bytes - bytes) / 1024 : null,
    };
    at = r.end + view * 6;
  }

  // The date rail let go far away.
  await page.waitForTimeout(1500);
  const height = await page.evaluate(() => document.querySelector('#scroller').scrollHeight);
  results.jump = await page.evaluate(([top, limit]) => window.__jump(top, limit),
    [Math.min(height - view * 2, at + view * 40), 15000]);
  await context.close();
  if (link) link.close();
  return results;
}

function report(name, runs) {
  const label = name === 'phone'
    ? `phone (CPU 4x slower, ${RTT} ms round trip, ${arg('down', 2000)} kbit/s down)`
    : 'desktop (home network)';
  console.log(`\n${label}`);
  console.log('  pace      px/ms   photo  preview   EMPTY  frames w/ empty  p95 frame   over link');
  for (const pace of Object.keys(PROFILE[name].speeds)) {
    const rows = runs.map((r) => r[pace]);
    const m = (key) => rows.reduce((n, r) => n + (r[key] || 0), 0) / rows.length;
    console.log(`  ${pace.padEnd(9)} ${String(rows[0].speed).padStart(5)}  ${m('photo').toFixed(1).padStart(6)}%`
      + `  ${m('preview').toFixed(1).padStart(6)}%  ${m('empty').toFixed(1).padStart(6)}%`
      + `  ${m('emptyFrames').toFixed(1).padStart(14)}%  ${m('p95frame').toFixed(0).padStart(6)} ms`
      + `  ${rows[0].kb == null ? '' : `${m('kb').toFixed(0).padStart(7)} KB`}`);
  }
  const jumps = runs.map((r) => r.jump);
  console.log(`  jump far away: no empty tile after ${jumps.map((j) => (j.covered == null ? 'never' : `${Math.round(j.covered)} ms`)).join(', ')};`
    + ` every photograph after ${jumps.map((j) => (j.sharp == null ? '>15 s' : `${Math.round(j.sharp)} ms`)).join(', ')}`);
}

async function main() {
  const browser = await launch();
  const all = {};
  for (const name of PROFILES) {
    all[name] = [];
    for (let run = 0; run < RUNS; run += 1) all[name].push(await runProfile(browser, name));
  }
  await browser.close();
  for (const name of PROFILES) report(name, all[name]);
  if (process.env.BENCH_JSON) console.log(JSON.stringify(all));
}

main().catch((error) => { console.error(error); process.exit(1); });
