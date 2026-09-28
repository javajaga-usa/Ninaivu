import { launch, ok, done, HOME, signInAtHome } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 390, height: 844 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(page);
await page.waitForSelector('#scroller', { state: 'attached', timeout: 15000 });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
await page.waitForTimeout(300);
await page.click('#grid .cell');
await page.waitForSelector('.viewer:not([hidden])', { timeout: 5000 }).catch(() => {});
await page.waitForTimeout(400);

// v-more should be visible, viewer-tools-more should NOT be open yet
const before = await page.evaluate(() => ({
  moreBtnVisible: getComputedStyle(document.querySelector('#v-more')).display !== 'none',
  menuOpen: document.querySelector('#viewer-tools-more').classList.contains('open'),
}));
ok('the More button is shown on a narrow viewport', before.moreBtnVisible);
ok('the menu starts closed', !before.menuOpen);

await page.click('#v-more');
await page.waitForTimeout(200);
await page.screenshot({ path: '/tmp/repro_viewer_more_open.png' });

const opened = await page.evaluate(() => {
  const menu = document.querySelector('#viewer-tools-more');
  const r = menu.getBoundingClientRect();
  return {
    open: menu.classList.contains('open'),
    withinViewport: r.left >= 0 && r.right <= window.innerWidth,
    rect: { left: r.left, right: r.right, top: r.top },
    favVisible: getComputedStyle(document.querySelector('#v-fav')).display !== 'none',
  };
});
ok('clicking More opens the dropdown', opened.open);
ok('the dropdown stays within the viewport', opened.withinViewport, JSON.stringify(opened.rect));
ok('Favourite (a folded action) is reachable inside the menu', opened.favVisible);

// clicking a button inside the menu closes it (rotate, harmless no-op-ish for a still image display check)
await page.click('#v-fav');
await page.waitForTimeout(150);
const afterPick = await page.evaluate(() => document.querySelector('#viewer-tools-more').classList.contains('open'));
ok('picking an action inside the menu closes it', !afterPick);

// re-open then close via outside click
await page.click('#v-more');
await page.waitForTimeout(150);
await page.mouse.click(195, 700);
await page.waitForTimeout(150);
const afterOutside = await page.evaluate(() => document.querySelector('#viewer-tools-more').classList.contains('open'));
ok('clicking outside the menu closes it', !afterOutside);

// re-open then close via Escape
await page.click('#v-more');
await page.waitForTimeout(150);
await page.keyboard.press('Escape');
await page.waitForTimeout(150);
const afterEscape = await page.evaluate(() => ({
  menuOpen: document.querySelector('#viewer-tools-more').classList.contains('open'),
  viewerStillOpen: !document.querySelector('.viewer').hidden,
}));
ok('Escape closes the menu without closing the viewer', !afterEscape.menuOpen && afterEscape.viewerStillOpen,
   JSON.stringify(afterEscape));

ok('no console errors', errs.length === 0, errs.join('; '));
await done(b);
