/** Browser regressions with isolated responses; no household data is modified. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { launch } from './harness.mjs';

const browser = await launch();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const template = readFileSync(new URL('../ninaivu/templates/share.html', import.meta.url), 'utf8')
    .replaceAll('{{ app_name }}', 'Ninaivu').replaceAll('{{ token }}', 'test-token')
    .replace("{{ asset('/static/js/share.js') }}", '/static/js/share.js');
  const script = readFileSync(new URL('../ninaivu/static/js/share.js', import.meta.url), 'utf8');
  // share.js is a module now — the share page is translated, so it imports
  // i18n.js — and this route table aborts anything it does not name. Without
  // this the import fails, the module never runs, and the page has no fields.
  const i18n = readFileSync(new URL('../ninaivu/static/js/i18n.js', import.meta.url), 'utf8');
  let unlocked = false;
  await page.route('http://ninaivu.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/share/test-token') return route.fulfill({ contentType: 'text/html', body: template,
      headers: { 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'" } });
    if (path === '/static/js/share.js') return route.fulfill({ contentType: 'text/javascript', body: script });
    if (path === '/static/js/i18n.js') return route.fulfill({ contentType: 'text/javascript', body: i18n });
    // English needs no locale file — every key is its own English — but the
    // module asks once and an aborted request is an error in the console.
    if (path.startsWith('/static/i18n/')) return route.fulfill({ json: {} });
    if (path.endsWith('/unlock')) { unlocked = true; return route.fulfill({ json: { ok: true } }); }
    if (path === '/api/share/test-token') return route.fulfill({ status: unlocked ? 200 : 401,
      json: unlocked ? { scope: 'asset', item: { kind: 'picture', filename: 'photo.tiff', src: '/original.tiff', view: '/preview.svg' } } : { password_required: true } });
    if (path === '/preview.svg') return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60"><rect width="80" height="60" fill="orange"/></svg>' });
    return route.abort();
  });
  await page.goto('http://ninaivu.test/share/test-token');
  await page.getByLabel('Share password').fill('test-password');
  await page.getByRole('button', { name: 'Open', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.single img')?.naturalWidth > 0);
  assert.equal(await page.locator('.single img').getAttribute('src'), '/preview.svg');
  assert.deepEqual(errors, []);
  console.log('PASS: shared password unlock and photo rendering under strict CSP');

  const family = await browser.newPage();
  await family.setContent('<div class="shell mobile-open"></div><div id="people-block"><div id="people-row"></div></div><input id="search"><button id="clear-search"></button><div id="suggestions"></div><button data-view="all"></button><button data-view="videos" class="active"></button>');
  const app = readFileSync(new URL('../ninaivu/static/js/app.js', import.meta.url), 'utf8');
  const render = app.slice(app.indexOf('function renderPeople()'), app.indexOf('\nasync function loadPeople()'));
  await family.evaluate(render => {
    window.$ = selector => document.querySelector(selector);
    window.state = { view: 'videos', filters: { q: 'old search', folder: 'elsewhere', person: 1, sort: 'date_asc' },
      people: [{ id: 1, name: 'First', photo_count: 2 }, { id: '2', name: 'Second', photo_count: 3 }] };
    window.searchTimer = setTimeout(() => { state.filters.q = 'stale'; }, 500);
    window.syncChips = () => {};
    window.reload = () => { window.result = structuredClone(state); };
    (0, eval)(render);
    renderPeople();
  }, render);
  await family.getByRole('button', { name: 'Second' }).click();
  const result = await family.evaluate(() => window.result);
  assert.equal(result.view, 'all');
  assert.equal(result.filters.person, 2);
  assert.equal(result.filters.q, '');
  assert.equal(result.filters.folder, '');
  assert.equal(result.filters.sort, 'date_asc');
  await family.getByRole('button', { name: 'Second' }).click();
  assert.equal(await family.evaluate(() => window.result.filters.person), 0);
  console.log('PASS: family face clicks clear conflicting filters and toggle person selection');
} finally {
  await browser.close();
}
