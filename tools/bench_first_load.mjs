/**
 * How long the family app takes to open on a phone far from home.
 *
 * The family in India reach the Mac mini over Tailscale: a round trip of a
 * quarter of a second or more, and a few megabits a second on a good day.
 * Nothing on the home Wi-Fi shows what that costs, so this puts a slow link in
 * between and times what a person waits for:
 *
 *   cold     a phone that has never opened Ninaivu (or whose browser let the
 *            copy go): every file comes over the link
 *   warm     the next visit, with the service worker and its copies in place
 *   offline  the server cannot be reached at all: does anything still open?
 *
 * The link is a small TCP relay (tests/slow_link.mjs) rather than DevTools
 * throttling, because DevTools does not slow the service worker's own
 * requests, and the warm and offline cases are about exactly those.
 *
 * Usage (wants a family app up, and its admin's name and password):
 *
 *   node tools/bench_first_load.mjs [--rtt 300] [--down 2000] [--up 750] [--runs 3]
 *
 * --down and --up are in kilobits a second. NINAIVU_HOME, NINAIVU_USER and
 * NINAIVU_PASS are read the way the browser tests read them.
 */
import { launch, HOME, ADMIN_USER, ADMIN_PASS } from '../tests/harness.mjs';
import { slowLink } from '../tests/slow_link.mjs';

const args = process.argv.slice(2);
const arg = (name, fallback) => {
  const at = args.indexOf(`--${name}`);
  return at >= 0 ? Number(args[at + 1]) : fallback;
};
const RTT = arg('rtt', 300);
const DOWN = arg('down', 2000) * 1000 / 8;    // bytes a second
const UP = arg('up', 750) * 1000 / 8;
const RUNS = arg('runs', 3);

/** What the page has done by the time the gallery is usable. */
async function open(page, url) {
  const started = Date.now();
  await page.goto(url, { waitUntil: 'commit', timeout: 120000 });
  // The first photograph on screen: what a person is waiting to see.
  await page.waitForFunction(() => [...document.querySelectorAll('#grid img')]
    .some((img) => img.complete && img.naturalWidth > 0), null, { timeout: 120000, polling: 50 });
  const firstPhoto = Date.now() - started;
  const timing = await page.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0];
    const res = performance.getEntriesByType('resource');
    const sum = (list) => list.reduce((n, r) => n + (r.transferSize || 0), 0);
    const scripts = res.filter((r) => /\.m?js(\?|$)/.test(r.name));
    const styles = res.filter((r) => /\.css(\?|$)/.test(r.name));
    return {
      html: nav ? nav.transferSize : 0,
      pageArrived: nav ? Math.round(nav.responseEnd) : 0,
      scripts: scripts.length,
      scriptBytes: sum(scripts),
      scriptsDecoded: scripts.reduce((n, r) => n + (r.decodedBodySize || 0), 0),
      styleBytes: sum(styles),
      requests: res.length + 1,
      list: res.map((r) => [Math.round(r.startTime), Math.round(r.responseEnd), r.transferSize,
        r.name.replace(location.origin, '').slice(0, 70)]),
    };
  });
  return { firstPhoto, ...timing };
}

async function signIn(page, url) {
  await page.goto(url, { waitUntil: 'load', timeout: 120000 });
  await page.evaluate(async ([username, password]) => {
    await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
  }, [ADMIN_USER, ADMIN_PASS]);
}

const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];

async function main() {
  const link = await slowLink(HOME, { rtt: RTT, down: DOWN, up: UP });
  const browser = await launch();
  const phone = {
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true,
    userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 '
      + '(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
  };
  const rows = { cold: [], warm: [], offline: [] };
  for (let run = 0; run < RUNS; run += 1) {
    link.restore();
    const context = await browser.newContext(phone);
    const page = await context.newPage();
    await signIn(page, link.url);
    // Signing in visited the page once; start the cold visit from nothing but
    // the sign-in cookie, as a phone whose browser let its copies go would.
    const client = await context.newCDPSession(page);
    await client.send('Network.clearBrowserCache');
    await client.send('Storage.clearDataForOrigin',
      { origin: link.url, storageTypes: 'service_workers,cache_storage' });
    await page.goto('about:blank');

    const before = link.down.bytes;
    const cold = await open(page, link.url);
    cold.linkBytes = link.down.bytes - before;
    rows.cold.push(cold);

    // Let the worker take over and finish whatever it does after a visit.
    await page.evaluate(() => navigator.serviceWorker && navigator.serviceWorker.ready).catch(() => {});
    await page.waitForTimeout(8000);
    await page.goto('about:blank');
    const before2 = link.down.bytes;
    const warm = await open(page, link.url);
    warm.linkBytes = link.down.bytes - before2;
    rows.warm.push(warm);

    // Server gone: the shell should still open, with what it can show.
    await page.waitForTimeout(3000);
    await page.goto('about:blank');
    link.cut();
    const started = Date.now();
    await page.goto(link.url, { waitUntil: 'commit', timeout: 60000 }).catch(() => {});
    const shell = await page.waitForFunction(() => {
      if (/Ninaivu is Offline/.test(document.body?.textContent || '')) return 'offline page';
      if (document.querySelector('#topbar, .topbar, #grid') && document.querySelector('#boot')?.classList.contains('done')) return 'app';
      return false;
    }, null, { timeout: 30000, polling: 100 }).then((h) => h.jsonValue()).catch(() => 'nothing');
    rows.offline.push({ shows: shell, ms: Date.now() - started });
    await context.close();
  }
  await browser.close();
  link.close();

  console.log(`\nLink: ${RTT} ms round trip, ${arg('down', 2000)} kbit/s down, ${arg('up', 750)} kbit/s up; ${RUNS} runs, medians\n`);
  for (const name of ['cold', 'warm']) {
    const r = rows[name];
    const m = (key) => median(r.map((x) => x[key]));
    console.log(`${name.padEnd(6)} first photo ${String(m('firstPhoto')).padStart(6)} ms`
      + `  over the link ${(m('linkBytes') / 1024).toFixed(0).padStart(5)} KB`
      + `  scripts ${m('scripts')} (${(m('scriptBytes') / 1024).toFixed(0)} KB sent, ${(m('scriptsDecoded') / 1024).toFixed(0)} KB parsed)`
      + `  page in ${m('pageArrived')} ms`
      + `  styles ${(m('styleBytes') / 1024).toFixed(0)} KB  html ${(m('html') / 1024).toFixed(0)} KB  requests ${m('requests')}`);
  }
  console.log(`offline shows: ${rows.offline.map((o) => `${o.shows} (${o.ms} ms)`).join(', ')}`);
  if (process.env.BENCH_DUMP) {
    for (const name of ['cold', 'warm']) {
      console.log(`\n${name} timeline (start, end, bytes, url):`);
      rows[name][0].list.sort((a, b) => a[0] - b[0]).forEach((r) => console.log('  ', r.join('  ')));
    }
  }
  for (const name of ['cold', 'warm']) rows[name].forEach((r) => delete r.list);
  console.log(JSON.stringify(rows));
}

main().catch((error) => { console.error(error); process.exit(1); });
