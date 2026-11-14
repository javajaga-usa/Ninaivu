/**
 * The family app on a phone far from home: what it fetches, and what it keeps.
 *
 * Three promises from the "Speed up phone first load" work (static/sw.js,
 * static/js/shell-cache.js):
 *
 *   1. The photo editor is not part of opening the gallery. Its scripts and
 *      its stylesheet arrive the first time Edit is pressed, and the editor
 *      then looks the way it always did.
 *   2. On a slow link the second visit is shown the page kept from the first,
 *      without waiting on the server for it, and the modules the page may
 *      open later are in the worker's cache by then.
 *   3. A kept page from an older build is not left running: once the page
 *      asks, the worker says the server's page differs, and it reloads into
 *      the server's own.
 *
 *   node tests/shell_cache_ui.mjs   (wants the family app up)
 */
import { launch, ok, done, HOME, ADMIN_USER, ADMIN_PASS } from './harness.mjs';
import { slowLink } from './slow_link.mjs';

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function signIn(page, url) {
  await page.goto(url, { waitUntil: 'load' });
  await page.evaluate(async ([username, password]) => {
    await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
  }, [ADMIN_USER, ADMIN_PASS]);
}

const gallery = (page) => page.waitForFunction(() => [...document.querySelectorAll('#grid img')]
  .some((img) => img.complete && img.naturalWidth > 0), null, { timeout: 60000 });

const browser = await launch();

/* 1. The editor waits for Edit. ------------------------------------------ */
{
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  const page = await context.newPage();
  const asked = [];
  page.on('request', (request) => asked.push(new URL(request.url()).pathname));
  await signIn(page, HOME);
  await page.reload({ waitUntil: 'load' });
  await gallery(page);
  ok('opening the gallery does not fetch the editor',
    !asked.some((p) => p === '/static/js/editor.js' || p.startsWith('/static/js/studio/')),
    asked.filter((p) => /editor|studio/.test(p)).join(', '));
  ok('nor its stylesheet', !asked.includes('/static/css/editor.css'));
  const before = await page.evaluate(() => getComputedStyle(document.documentElement)
    .getPropertyValue('--pe-bg').trim());
  ok('the editor\'s colours are not in the page yet', before === '', before);

  await page.locator('#grid .cell').first().click();
  await page.waitForSelector('#viewer:not([hidden])');
  await page.locator('#v-edit').click();
  await page.waitForSelector('.photo-editor[open]', { timeout: 30000 });
  ok('Edit fetches the editor', asked.includes('/static/js/editor.js'));
  const styled = await page.evaluate(() => ({
    token: getComputedStyle(document.documentElement).getPropertyValue('--pe-bg').trim(),
    // Still in its own place among the sheets, so premium.css still wins
    // over it where it was written to.
    order: [...document.querySelectorAll('link[rel="stylesheet"]')]
      .map((l) => (l.getAttribute('href') || '').split('?')[0].split('/').pop()),
  }));
  ok('and its stylesheet, before the editor shows', styled.token !== '', JSON.stringify(styled));
  ok('which keeps its place among the sheets',
    styled.order.indexOf('editor.css') >= 0
      && styled.order.indexOf('editor.css') < styled.order.indexOf('premium.css'),
    styled.order.join(' '));
  await context.close();
}

/* 2 and 3. A slow link, and the kept page. ------------------------------- */
{
  const link = await slowLink(HOME, { rtt: 400, down: 400000, up: 150000 });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
  const page = await context.newPage();
  await signIn(page, link.url);
  await page.reload({ waitUntil: 'load' });
  await gallery(page);
  await page.evaluate(() => navigator.serviceWorker.ready);
  // The gallery settles, then the worker is handed the rest of the shell.
  const shellKeys = () => page.evaluate(async () => {
    const shell = (await caches.keys()).find((n) => n.startsWith('ninaivu-shell-'));
    if (!shell) return [];
    return (await (await caches.open(shell)).keys()).map((r) => new URL(r.url).pathname);
  });
  let keys = [];
  for (let i = 0; i < 120; i += 1) {
    keys = await shellKeys();
    if (keys.includes('/') && keys.includes('/static/js/editor.js')) break;
    await wait(500);
  }
  const warmed = keys.includes('/') && keys.includes('/static/js/editor.js') ? keys.length : 0;
  ok('the worker keeps the page and the modules opened later', warmed > 0, keys.join(' '));

  await page.goto('about:blank');
  await page.goto(link.url, { waitUntil: 'load' });
  const nav = await page.evaluate(() => {
    const entry = performance.getEntriesByType('navigation')[0];
    return { end: Math.round(entry.responseEnd - entry.startTime), worker: entry.workerStart > 0 };
  });
  // Two round trips (connect, ask) are 800 ms on this link; the kept page is
  // shown after the worker's short wait for the server instead.
  ok('the next visit is shown the kept page without waiting on the link',
    nav.worker && nav.end < 700, JSON.stringify(nav));
  await gallery(page);

  // Make the kept page an older build: one stamp in it changed.
  const real = await page.evaluate(() => document.querySelector('script[type="module"][src*="app.js"]').src);
  await page.evaluate(async () => {
    const shell = (await caches.keys()).find((n) => n.startsWith('ninaivu-shell-'));
    const cache = await caches.open(shell);
    for (const key of await cache.keys()) {
      if (new URL(key.url).pathname !== '/') continue;
      const kept = await cache.match(key);
      const text = (await kept.text()).replace(/(\/static\/css\/style\.css\?v=)[\w.-]+/, '$1older');
      await cache.put(key, new Response(text, { headers: kept.headers }));
    }
    sessionStorage.removeItem('ninaivu.shell-reloaded');
  });
  await page.goto('about:blank');
  await page.goto(link.url, { waitUntil: 'load' });
  const reloaded = await page.waitForFunction(() => {
    const at = Number(sessionStorage.getItem('ninaivu.shell-reloaded') || 0);
    const style = document.querySelector('link[href*="/static/css/style.css"]')?.getAttribute('href') || '';
    return at > 0 && !style.endsWith('older') && document.readyState === 'complete';
  }, null, { timeout: 30000, polling: 200 }).then(() => true).catch(() => false);
  ok('an older kept page reloads into the server\'s build', reloaded);
  const after = await page.evaluate(() => document.querySelector('script[type="module"][src*="app.js"]').src);
  ok('and runs the server\'s app', after === real, `${after} vs ${real}`);

  // Server gone: the kept page still opens.
  await page.goto('about:blank');
  link.cut();
  await page.goto(link.url, { waitUntil: 'commit' }).catch(() => {});
  const offline = await page.waitForFunction(() => Boolean(document.querySelector('#grid'))
    || /Ninaivu is Offline/.test(document.body?.textContent || ''), null, { timeout: 30000 })
    .then(() => page.evaluate(() => (document.querySelector('#grid') ? 'app' : 'offline page')))
    .catch(() => 'nothing');
  ok('with no server the kept page still opens', offline === 'app', offline);
  link.close();
  await wait(100);
  await context.close();
}

await done(browser);
