/**
 * The pages split out of app.js and admin.js in 4.0 (static/js/pages/): each
 * is fetched only when it is opened, at its stamped URL, and works once it is.
 *
 *   node tests/page_modules_ui.mjs      (wants an instance on 5000 and 3000)
 */
import { launch, ok, done, signInAtHome, signInAtConsole } from './harness.mjs';

const b = await launch();
const asked = [];
const errors = [];

/* ---------- the family app ---------- */
const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
p.on('pageerror', (e) => errors.push(e.message));
p.on('request', (r) => asked.push(r.url()));
await signInAtHome(p);
await p.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, null, { timeout: 30000 });
await p.waitForTimeout(500);
// Upload sits in the Create menu; pressed as the menu item would press it.
const press = (page, selector) => page.evaluate((sel) => document.querySelector(sel).click(), selector);
const pageAsked = (name) => asked.filter((url) => url.includes(`/static/js/pages/${name}`));

ok('no family page module is fetched at start', pageAsked('family/').length === 0,
  pageAsked('family/').join(' '));

await press(p, '#upload-btn');
await p.waitForSelector('#upload-modal:not([hidden])', { timeout: 8000 });
ok('Upload opens its dialog', await p.isVisible('#upload-modal'));
ok('the upload module was fetched stamped', pageAsked('family/upload.js').some((u) => /\?v=/.test(u)),
  pageAsked('family/upload.js').join(' '));
await p.click('#upload-close');
ok('Done closes it', await p.isHidden('#upload-modal'));

await press(p, '#map-btn');
await p.waitForSelector('#map-modal:not([hidden])', { timeout: 8000 });
ok('Map opens its dialog', await p.isVisible('#map-modal'));
ok('the map module was fetched once', pageAsked('family/map.js').length === 1, String(pageAsked('family/map.js').length));
await p.click('#map-close');
ok('closing the map hides it', await p.isHidden('#map-modal'));
await press(p, '#map-btn');
await p.waitForSelector('#map-modal:not([hidden])', { timeout: 8000 });
ok('a second opening fetches nothing again', pageAsked('family/map.js').length === 1);
await p.click('#map-close');

await p.locator('#grid .cell').first().click();
await p.waitForSelector('#viewer:not([hidden])', { timeout: 8000 }).catch(() => {});
const share = await p.$('#v-share');
if (share && await share.isVisible()) {
  await share.click();
} else {
  // On a narrow viewer the share button sits in the More menu.
  await p.evaluate(() => document.querySelector('#v-share')?.click());
}
await p.waitForSelector('#share-modal:not([hidden])', { timeout: 8000 });
ok('Share link from the viewer opens the share dialog', await p.isVisible('#share-modal'));
ok('with its options, not an old link', await p.isVisible('#share-options')
  && await p.isHidden('#share-link-result'));
await p.click('#share-close');
ok('closing it hides it', await p.isHidden('#share-modal'));

ok('the duplicates review is not fetched for the ordinary gallery', pageAsked('family/duplicates.js').length === 0);
await p.keyboard.press('Escape');          // the viewer the share came from
await p.waitForTimeout(300);
await press(p, '.side-item[data-view="duplicates"]');
await p.waitForTimeout(1500);
ok('opening Duplicates fetches its review', pageAsked('family/duplicates.js').length === 1);
const banner = await p.$eval('#duplicates-banner', (n) => ({ hidden: n.hidden, text: n.innerText.trim() }));
ok('and its banner is drawn or put away, never left half-done', banner.hidden || banner.text.length > 0,
  JSON.stringify(banner));
await press(p, '.side-item[data-view="all"]');
await p.waitForTimeout(800);
ok('leaving Duplicates puts the banner away', await p.$eval('#duplicates-banner', (n) => n.hidden));

/* ---------- the console ---------- */
const c = await b.newPage({ viewport: { width: 1400, height: 900 } });
c.on('pageerror', (e) => errors.push(e.message));
c.on('request', (r) => asked.push(r.url()));
await signInAtConsole(c);
await c.waitForTimeout(800);
// common.js is what the console shell shares with its pages; it comes at start.
const consolePages = () => pageAsked('console/').filter((url) => !url.includes('/common.js'));
ok('no console page module is fetched at start', consolePages().length === 0, consolePages().join(' '));
await c.click('#tabs button[data-tab="large-files"]');
await c.waitForSelector('[data-panel="large-files"].active');
await c.waitForTimeout(1200);
ok('Large files was fetched when opened, stamped',
  pageAsked('console/large-files.js').some((u) => /\?v=/.test(u)), pageAsked('console/large-files.js').join(' '));
ok('and its Refresh is wired', await c.evaluate(() => typeof document.querySelector('#lf-refresh')?.onclick === 'function'));

// The command bar opens a page and presses its button straight away, before
// the page's module can have come: Health's "Back up now" must still work.
await c.evaluate(() => {
  document.querySelector('#tabs button[data-tab="health"]').click();
  setTimeout(() => document.querySelector('#backup-now').click(), 0);
});
await c.waitForFunction(() => [...document.querySelectorAll('#toasts .toast')]
  .some((t) => /copy of the index has been kept/.test(t.textContent)), null, { timeout: 15000 })
  .then(() => ok('Back up now, pressed as the page opens, keeps a copy', true))
  .catch(() => ok('Back up now, pressed as the page opens, keeps a copy', false));
ok('Health was fetched when opened', pageAsked('console/health.js').length === 1);
ok('and its problems Refresh is wired',
  await c.evaluate(() => typeof document.querySelector('#problem-refresh')?.onclick === 'function'));

ok('no script errors', errors.length === 0, errors.join(' | '));
await done(b);
