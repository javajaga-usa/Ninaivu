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

/** Fetched once, at a stamped URL. */
const familyOnce = (name) => {
  const urls = pageAsked(`family/${name}.js`);
  ok(`${name} was fetched once, stamped`, urls.length === 1 && /\?v=/.test(urls[0]), urls.join(' '));
};

// Jump to: the button is there from the start, its list comes when opened.
await p.waitForSelector('#jump:not([hidden])', { timeout: 15000 }).catch(() => {});
if (await p.isVisible('#jump')) {
  ok('the Jump to list is not fetched before it is opened', pageAsked('family/jump.js').length === 0);
  await p.click('#jump-btn');
  await p.waitForSelector('#jump-pop:not([hidden])', { timeout: 8000 });
  ok('Jump to lists the years', await p.$$eval('#jump-years button', (n) => n.length) > 0);
  ok('and the months, some to choose', await p.$$eval('#jump-months button:not(:disabled)', (n) => n.length) > 0);
  await p.keyboard.press('Escape');
  ok('Escape closes it', await p.isHidden('#jump-pop'));
  await p.click('#jump-btn');
  await p.waitForSelector('#jump-pop:not([hidden])', { timeout: 8000 });
  await p.click('#jump-months button:not(:disabled)');
  ok('choosing a month closes it', await p.isHidden('#jump-pop'));
  familyOnce('jump');
} else {
  ok('Jump to is shown for this library', false);
}

/* ---------- the console ---------- */
const c = await b.newPage({ viewport: { width: 1400, height: 900 } });
c.on('pageerror', (e) => errors.push(e.message));
c.on('request', (r) => asked.push(r.url()));
await signInAtConsole(c);
await c.waitForTimeout(800);
// common.js is what the console shell shares with its pages; it comes at start.
// People is loaded at sign-in, to draw the list sign-in fetches (the preview
// needs that list too): fetched then, once, and nothing else is.
const consolePages = () => pageAsked('console/')
  .filter((url) => !url.includes('/common.js') && !url.includes('/people.js'));
ok('no console page module is fetched at start', consolePages().length === 0, consolePages().join(' '));
ok('People is fetched at sign-in, once, stamped', pageAsked('console/people.js').length === 1
  && /\?v=/.test(pageAsked('console/people.js')[0]), pageAsked('console/people.js').join(' '));
ok('and has drawn everyone', await c.$$eval('.admin-person', (n) => n.length) > 0);
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

// Notifications and the storage check are asked for at sign-in; their modules
// come with the Health page and draw what sign-in fetched.
const stampedOnce = (name) => {
  const urls = pageAsked(`console/${name}.js`);
  ok(`${name} was fetched once, stamped`, urls.length === 1 && /\?v=/.test(urls[0]), urls.join(' '));
};
await c.waitForFunction(() => document.querySelectorAll('#notify-events input[type="checkbox"]').length > 0,
  null, { timeout: 8000 }).catch(() => {});
ok('the notification events are drawn on Health',
  await c.$$eval('#notify-events input[type="checkbox"]', (n) => n.length) > 0);
ok('and the line under them says something', (await c.textContent('#notify-state')).trim().length > 0);
ok('the storage check shows its count', /^\d+$/.test((await c.textContent('#scrub-total')).trim()));
await c.click('#tabs button[data-tab="overview"]');
await c.waitForTimeout(300);
await c.click('#tabs button[data-tab="health"]');
await c.waitForTimeout(800);
stampedOnce('notifications');
stampedOnce('scrubber');

/** Open console page *tab*, and once more after another, to see it fetched once. */
async function openConsolePage(tab, ready) {
  ok(`${tab} is not fetched before it is opened`, pageAsked(`console/${tab}.js`).length === 0);
  await c.click(`#tabs button[data-tab="${tab}"]`);
  await c.waitForSelector(`[data-panel="${tab}"].active`);
  await c.waitForFunction(ready, null, { timeout: 10000 })
    .then(() => ok(`${tab} draws when opened`, true))
    .catch(() => ok(`${tab} draws when opened`, false));
  await c.click('#tabs button[data-tab="overview"]');
  await c.waitForTimeout(200);
  await c.click(`#tabs button[data-tab="${tab}"]`);
  await c.waitForTimeout(600);
  stampedOnce(tab);
}

await openConsolePage('library', () => document.querySelectorAll('#library-list .library-item').length > 0
  && !/…/.test(document.querySelector('#loc-summary')?.textContent || '…'));
// The command bar opens People and presses Add person in the same moment.
await c.evaluate(() => {
  document.querySelector('#tabs button[data-tab="people"]').click();
  setTimeout(() => document.querySelector('#add-person-btn').click(), 0);
});
await c.waitForSelector('#add-person-block:not([hidden])', { timeout: 8000 })
  .then(() => ok('Add person, pressed as People opens, opens the form', true))
  .catch(() => ok('Add person, pressed as People opens, opens the form', false));
ok('People is still fetched only once', pageAsked('console/people.js').length === 1);

ok('and Library\'s "Change" is still wired from the start',
  await c.evaluate(() => typeof document.querySelector('#change-root')?.onclick === 'function'));

// The uploads queue is asked for at sign-in and polled; its list is drawn by
// the Uploads module once the page is opened.
await openConsolePage('uploads', () => document.querySelector('#pending-uploads')?.children.length > 0
  && document.querySelector('#pb-people')?.children.length > 0);
ok('Uploads draws the queue or says it is empty',
  (await c.textContent('#pending-uploads')).trim().length > 0);
ok('and Refresh is wired', await c.evaluate(() => typeof document.querySelector('#uploads-refresh')?.onclick === 'function'));
// The Server page's TV album comes with the same module, not fetched again.
await c.click('#tabs button[data-tab="server"]');
await c.waitForFunction(() => document.querySelectorAll('#tv-album-select option').length > 0,
  null, { timeout: 10000 }).catch(() => {});
ok('the Server page fills the TV album choices', await c.$$eval('#tv-album-select option', (n) => n.length) > 0);
ok('from the module already loaded', pageAsked('console/uploads.js').length === 1);

ok('no script errors', errors.length === 0, errors.join(' | '));
await done(b);
